# 网络搜索与网页访问能力规划

## 目标范围

当前阶段仅实现：

- 网络搜索能力
- 网页访问与正文读取能力
- 公共互联网内容获取
- 与现有 Tool Runtime 的安全集成

当前阶段不包含：

- 浏览器自动化
- Playwright/Selenium
- 登录态管理
- OAuth
- 多标签页浏览器会话
- Office/RAG/向量数据库
- 多 Agent 编排

## 建议新增工具

### web.search

用途：

- 联网搜索公开信息
- 查询技术文档
- 查询错误与 API 文档

建议参数：

```json
{
  "query": "python asyncio tutorial",
  "limit": 5
}
```

建议返回：

```json
{
  "query": "python asyncio tutorial",
  "results": [
    {
      "title": "Asyncio Documentation",
      "url": "https://docs.python.org/...",
      "snippet": "Coroutines and Tasks ..."
    }
  ]
}
```

建议风险等级：

- R0
- OBSERVE

### web.fetch

用途：

- 获取网页正文
- 提取网页文本
- 读取技术文档

建议参数：

```json
{
  "url": "https://docs.python.org/3/library/asyncio.html"
}
```

建议返回：

```json
{
  "url": "https://docs.python.org/3/library/asyncio.html",
  "status": 200,
  "title": "asyncio — Asynchronous I/O",
  "content_type": "text/html",
  "text": "..."
}
```

建议风险等级：

- R0
- OBSERVE

### web.extract

用途：

- HTML 正文提取
- Markdown 转换
- 去除导航与广告内容

建议风险等级：

- R0
- OBSERVE

### web.download

用途：

- 下载公开文件到 workspace
- 下载 HTML/PDF/文本资源

建议风险等级：

- R1
- MUTATE

原因：

- 会修改 workspace
- 应接入 mutation journal
- 应支持 rollback

## 建议架构

建议新增目录：

```text
src/tsm_agt/adapters/network/
    __init__.py
    http_client.py
    search_provider.py
    content_extractor.py

src/tsm_agt/adapters/builtin/
    web_tools.py
```

## 安全边界

建议新增：

```python
class NetworkAccessPolicy:
```

建议限制：

- 禁止 localhost
- 禁止 file://
- 禁止内网 IP
- 限制响应大小
- 限制 redirect 次数
- 限制下载文件大小

默认建议：

```python
allow_private_network = False
```

目标：

- 防止 SSRF
- 防止读取本地资源
- 防止访问 Runtime 内部服务

## 技术建议

推荐依赖：

```toml
httpx
trafilatura
beautifulsoup4
```

## MVP 顺序

### 第一阶段

仅实现：

- web.search
- web.fetch

能力目标：

- 联网搜索
- 网页正文读取
- 官方文档访问
- 错误信息搜索

### 第二阶段

增加：

- web.download
- robots policy
- rate limit
- cache

### 第三阶段

后续再考虑：

- browser automation
- Playwright
- 登录态
- 页面操作

## Runtime 集成详细设计

### 总体架构

建议采用分层结构：

```text
LLM / Agent Layer
    ↓
Capability Runtime Layer
    ↓
Network Policy Layer
    ↓
Provider Adapter Layer
    ↓
HTTP Client Layer
```

各层职责：

#### LLM / Agent Layer

负责：

- 发起联网请求
- 组织 evidence_question
- 消费搜索结果
- 基于搜索结果继续推理

不负责：

- 网络权限判断
- URL 安全校验
- 缓存控制
- provider fallback

#### Capability Runtime Layer

建议新增：

```text
src/tsm_agt/runtime/network_state.py
```

建议核心对象：

```python
class NetworkCapabilityState:
```

建议维护：

```python
network_mode
allowed_domains
blocked_domains
request_budget
fetch_budget
download_budget
visited_urls
search_history
```

作用：

- 控制联网能力生命周期
- 避免重复搜索
- 控制请求预算
- 管理联网阶段状态

#### Network Policy Layer

负责：

- URL 安全校验
- SSRF 防护
- 域名限制
- Content-Type 检查
- 响应大小限制
- Redirect 限制

建议新增：

```text
src/tsm_agt/security/network_policy.py
```

建议接口：

```python
class NetworkAccessPolicy:
    def validate_url(self, url: str) -> ValidationResult:
        ...

    def validate_response(self, response: HttpResponse) -> None:
        ...
```

#### Provider Adapter Layer

负责：

- 对接不同搜索 provider
- 统一返回结构
- 处理 provider 限流
- provider fallback

建议结构：

```text
src/tsm_agt/adapters/network/providers/
    base.py
    duckduckgo.py
    brave.py
    serpapi.py
```

建议统一接口：

```python
class SearchProvider(Protocol):
    async def search(
        self,
        query: str,
        limit: int,
    ) -> SearchResult:
        ...
```

#### HTTP Client Layer

负责：

- 连接复用
- 超时控制
- retry
- gzip
- streaming
- SSL 校验

建议统一封装：

```python
class RuntimeHttpClient:
```

避免 tool 直接调用 httpx。

## Tool 详细设计

### web.search

#### 调用流程

```text
LLM
  ↓
web.search
  ↓
NetworkCapabilityState 检查
  ↓
NetworkAccessPolicy 校验
  ↓
SearchProvider 调用
  ↓
结果标准化
  ↓
返回 Runtime
```

#### 建议输入结构

```json
{
  "query": "python asyncio gather",
  "limit": 5,
  "provider": "auto",
  "freshness": "default"
}
```

#### 建议输出结构

```json
{
  "query": "python asyncio gather",
  "provider": "duckduckgo",
  "results": [
    {
      "title": "Coroutines and Tasks",
      "url": "https://docs.python.org/...",
      "snippet": "Run awaitable objects concurrently",
      "rank": 1,
      "source": "search"
    }
  ],
  "truncated": false
}
```

#### 关键设计点

建议：

- 默认限制结果数量
- 禁止返回超长 snippet
- URL 必须标准化
- 保留 provider 来源
- 支持 provider fallback

### web.fetch

#### 调用流程

```text
URL 输入
  ↓
URL Validation
  ↓
HTTP Fetch
  ↓
Content-Type Validation
  ↓
HTML Extraction
  ↓
正文裁剪
  ↓
返回文本
```

#### 建议输入结构

```json
{
  "url": "https://docs.python.org/3/library/asyncio.html",
  "max_chars": 20000
}
```

#### 建议输出结构

```json
{
  "url": "https://docs.python.org/3/library/asyncio.html",
  "status": 200,
  "title": "asyncio — Asynchronous I/O",
  "content_type": "text/html",
  "text": "...",
  "truncated": true
}
```

#### 内容提取建议

建议：

- 优先正文提取
- 去除导航栏
- 去除脚本内容
- 去除广告区域
- 控制最大 token

建议流程：

```text
raw html
  ↓
trafilatura
  ↓
fallback: beautifulsoup
  ↓
markdown/text normalize
```

### web.download

#### 风险控制

由于会写入 workspace：

- 必须接入 mutation journal
- 必须支持 rollback
- 必须限制下载目录
- 必须校验文件类型

建议仅允许：

```text
workspace/downloads/
```

避免任意路径写入。

#### 建议下载流程

```text
URL Validation
  ↓
HEAD Request
  ↓
Size Validation
  ↓
Download Stream
  ↓
Workspace Write
  ↓
Mutation Journal
```

## 数据结构设计

### SearchResult

建议：

```python
@dataclass
class SearchResultItem:
    title: str
    url: str
    snippet: str
    rank: int
    provider: str
```

```python
@dataclass
class SearchResult:
    query: str
    results: list[SearchResultItem]
    provider: str
    truncated: bool
```

### FetchResult

```python
@dataclass
class FetchResult:
    url: str
    status: int
    title: str | None
    content_type: str
    text: str
    truncated: bool
```

### NetworkRequestMetadata

```python
@dataclass
class NetworkRequestMetadata:
    request_id: str
    start_time: float
    duration_ms: int
    redirect_count: int
    response_size: int
```

## Runtime 状态管理

### 联网阶段控制

建议增加：

```python
class NetworkPhase(Enum):
    DISCOVERY
    DOCUMENT_LOOKUP
    VALIDATION
    DOWNLOAD
```

不同阶段暴露不同能力。

例如：

#### DISCOVERY

允许：

- web.search

限制：

- web.download

#### DOCUMENT_LOOKUP

允许：

- web.search
- web.fetch

限制：

- 大文件下载

#### DOWNLOAD

允许：

- web.download

增加：

- mutation 审批
- 文件大小检查

## 与现有 Runtime 的兼容设计

### 与 evidence_question 集成

建议：

联网工具必须继续要求：

```json
{
  "evidence_question": {
    "question_id": "Q1",
    "question": "需要通过官方文档确认 asyncio gather 的行为"
  }
}
```

目的：

- 保持搜索目标明确
- 防止无目标联网
- 保持 investigation traceability

### 与 exploration budget 集成

建议：

联网搜索纳入统一 budget。

例如：

```python
max_network_search_per_turn = 3
max_fetch_per_turn = 5
```

避免：

- 无限联网
- token 爆炸
- retrieval noise

### 与 capability exposure 集成

建议：

Runtime 根据当前 phase：

动态暴露：

```text
web.search
web.fetch
web.download
```

而不是永久暴露全部工具。

## 异常处理设计

### 建议统一异常模型

```python
class NetworkToolError(ToolError):
    pass
```

细分：

```python
InvalidUrlError
PrivateNetworkBlockedError
ResponseTooLargeError
ProviderRateLimitError
FetchTimeoutError
UnsupportedContentTypeError
```

### Tool 返回建议

建议不要直接暴露底层异常。

建议统一：

```json
{
  "error": {
    "type": "FETCH_TIMEOUT",
    "message": "Request timed out"
  }
}
```

## 可扩展性设计

### Provider 可扩展

通过 Provider Adapter：

后续可增加：

- Brave Search
- Tavily
- SerpAPI
- Bing Search
- 内部知识搜索

无需修改 Runtime 主流程。

### 内容解析可扩展

后续可增加：

- PDF extraction
- Markdown rendering
- XML parsing
- RSS parsing

### Browser 能力扩展

未来如果增加 Playwright：

建议新增独立 capability：

```text
browser.navigate
browser.click
browser.extract
```

不要与 web.fetch 混合。

## 推荐实现顺序

### 第一阶段（MVP）

实现：

- RuntimeHttpClient
- NetworkAccessPolicy
- web.search
- web.fetch
- HTML 正文提取

目标：

- 官方文档访问
- 错误检索
- 联网 evidence 获取

### 第二阶段

实现：

- provider fallback
- cache
- robots policy
- rate limit
- runtime budget

### 第三阶段

实现：

- web.download
- mutation integration
- file type validation
- streaming download

### 第四阶段

后续再考虑：

- browser automation
- authenticated session
- persistent browsing state
- multi-page navigation
