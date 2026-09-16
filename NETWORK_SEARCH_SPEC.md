# 联网搜索能力实现 SPEC

版本：v1.0
状态：Draft

## 1. SPEC 目标

本 SPEC 用于定义当前项目后续的联网搜索能力实现规范，包括：

- Web Search
- Web Fetch
- 内容摘要
- 搜索结果标准化
- 安全网络访问
- Agent 化联网搜索扩展

目标：

- 提供稳定的联网搜索能力
- 提供安全可控的网页抓取能力
- 提供统一的搜索结果结构
- 为后续多 Provider、Agent 搜索能力扩展提供基础

---

## 2. 当前项目已有能力

当前项目已存在：

- `web.search`
- `web.fetch_markdown`
- `content.summarize`

核心实现位置：

```text
src/tsm_agt/adapters/builtin/network_tools.py
```

当前能力：

- DuckDuckGo 搜索
- HTML fallback 搜索
- 基础 snippet 提取
- 基础网页抓取
- 内容摘要
- fetch 默认关闭

当前限制：

- Provider 单一
- SSRF 防护不完整
- 缺少统一 NetworkAccessPolicy
- 缺少 freshness/trust ranking
- 缺少正文抽取
- 缺少 Agent 多跳搜索能力

---

## 3. 能力边界

联网搜索模块负责：

- 搜索请求处理
- Provider 调用
- 网页抓取
- 正文提取
- 搜索结果标准化
- 安全校验
- 网络访问控制
- 搜索缓存
- 可观测性

不负责：

- 最终业务推理
- 用户权限系统
- 长期知识库存储

---

## 4. 核心能力

### 4.1 Web Search

支持：

- 通用网页搜索
- 新闻搜索
- 多 Provider 搜索
- Provider fallback

### 4.2 Web Fetch

支持：

- HTML 页面抓取
- Markdown 转换
- 正文提取
- 页面元信息提取
- Redirect 处理

### 4.3 Content Summarization

支持：

- 搜索结果摘要
- 网页内容摘要
- 多来源摘要
- 长文压缩

### 4.4 Agentic Search

后续支持：

- 多跳搜索
- 自动继续搜索
- 自动打开结果页
- 搜索链路推理
- 引用链输出

---

## 5. 推荐架构

```text
network/
├── providers/
├── fetch/
├── parsing/
├── extraction/
├── security/
├── ranking/
├── summarize/
├── cache/
├── telemetry/
└── models/
```

---

## 6. Provider 规范

### 6.1 Provider 抽象

```python
class SearchProvider:
    def search(self, request: SearchRequest) -> SearchResponse:
        ...
```

### 6.2 推荐 Provider

建议支持：

- DuckDuckGo
- Brave Search
- Bing Search
- SerpAPI
- Hacker News
- GitHub Search
- Reddit Search

---

## 7. 搜索输入结构

```python
@dataclass
class SearchRequest:
    query: str
    top_k: int
    language: str | None
    region: str | None
    freshness: str | None
    safe_search: bool
    provider_preferences: list[str]
    timeout_seconds: int
```

---

## 8. 搜索输出结构

```python
@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    provider: str
    domain: str
    published_at: datetime | None
    trust_score: float | None
    freshness_score: float | None
```

```python
@dataclass
class SearchResponse:
    query: str
    results: list[SearchResult]
    providers_used: list[str]
    latency_ms: int
    partial_failure: bool
```

---

## 9. 搜索流程

```text
用户请求
→ query normalize
→ provider select
→ provider search
→ normalize
→ deduplicate
→ ranking
→ summarize
→ output
```

---

## 10. 搜索结果处理

### 去重

支持：

- URL 去重
- 域名去重
- 语义去重

### 排序

综合：

- relevance_score
- freshness_score
- trust_score
- source_diversity_score

### 来源可信度

维护：

- domain_reputation
- blacklist
- whitelist
- source_type

---

## 11. 网页抓取规范

支持：

- HTML 获取
- redirect
- charset
- gzip
- markdown conversion

正文提取建议：

- readability
- trafilatura

---

## 12. 安全规范

必须实现：

```python
NetworkAccessPolicy
```

### SSRF 防护

仅允许：

- http
- https

禁止：

- file://
- ftp://
- smb://
- gopher://
- data://

### 内网地址拦截

必须阻止：

- 127.0.0.0/8
- 10.0.0.0/8
- 172.16.0.0/12
- 192.168.0.0/16
- localhost
- metadata endpoint
- IPv6 local addresses

### Redirect 校验

每次 redirect 后必须重新：

- DNS resolve
- IP validate
- protocol validate

### Response 限制

支持：

```python
MAX_RESPONSE_SIZE
MAX_REDIRECTS
MAX_FETCH_SECONDS
```

### Content-Type 白名单

允许：

- text/html
- text/plain
- text/markdown
- application/xhtml+xml

拒绝：

- application/octet-stream
- video/*
- audio/*
- application/zip

---

## 13. HTTP 基础设施

推荐迁移：

```text
httpx
```

要求支持：

- timeout
- retry
- connection pooling
- HTTP/2
- sync/async

---

## 14. 缓存规范

### 搜索缓存

缓存 Key：

```python
(query, provider, freshness)
```

支持：

- TTL
- stale cache
- partial cache

### 页面缓存

支持：

- ETag
- Last-Modified

---

## 15. 可观测性

### 日志

必须记录：

- query
- provider
- timeout
- retry
- blocked request
- fetch failure

禁止记录：

- token
- cookie
- credential

### Metrics

支持：

- search_latency
- provider_failure_rate
- fetch_timeout_rate
- cache_hit_rate
- blocked_request_count

---

## 16. 错误处理

标准错误：

```python
SearchProviderError
NetworkPolicyError
FetchTimeoutError
RateLimitError
ContentExtractionError
```

---

## 17. 降级策略

支持：

- provider fallback
- partial results
- graceful degradation

---

## 18. 引用系统

输出支持：

```python
citation_url
citation_title
citation_provider
```

---

## 19. 推荐配置项

```python
SEARCH_TIMEOUT
FETCH_TIMEOUT
MAX_RESULTS
MAX_REDIRECTS
ENABLE_FETCH
ENABLE_PROVIDER_FALLBACK
SAFE_SEARCH
CACHE_TTL
```

---

## 20. 测试规范

### 单元测试

覆盖：

- parser
- provider
- ranking
- SSRF
- redirect
- extraction

### 集成测试

覆盖：

- provider fallback
- timeout
- partial failure
- cache

### 安全测试

覆盖：

- localhost SSRF
- redirect SSRF
- DNS rebinding
- oversized response
- invalid content-type

---

## 21. 推荐实施阶段

### Phase 1

目标：

提升搜索结果质量

内容：

- 真实 snippet
- SearchResult 标准化
- Provider fallback

### Phase 2

目标：

安全开放 fetch

内容：

- NetworkAccessPolicy
- SSRF 防护
- redirect 校验
- response 限制

### Phase 3

目标：

增强网页阅读能力

内容：

- 正文提取
- Markdown 转换
- 元信息提取

### Phase 4

目标：

提升搜索稳定性

内容：

- 多 Provider
- freshness ranking
- trust score
- semantic deduplication

### Phase 5

目标：

Agent 化联网搜索

内容：

- 多跳搜索
- 自动浏览
- 搜索规划
- 引用链
- 信息交叉验证

---

## 22. 最终目标

系统最终从：

```text
基础 web.search tool
```

演进为：

```text
安全、可扩展、可观测、具备 Agent 推理能力的联网搜索系统
```
