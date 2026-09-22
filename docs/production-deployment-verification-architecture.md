# 生产级部署与验证体系设计

## 1. 目标与问题背景

本文档定义一套适用于多人同时使用服务场景的生产级部署与验证体系，用于解决以下高频问题：

- UI 修改本地生效但线上未生效
- HTML、CSS、JS 版本不一致
- CDN、浏览器或 Service Worker 缓存污染
- reload 后旧进程继续提供旧页面
- 多实例滚动发布过程中用户命中混合版本
- CI/CD 只完成构建但未验证真实页面
- design token 演进失控导致样式漂移
- Runtime verification 缺失导致 no-progress safety stop
- 灰度与回滚过程中旧资源被提前删除
- E2E 未覆盖真实用户视角的样式验证

本文档重点覆盖：

- 静态资源 hash/version 管理
- HTML、CDN、浏览器与 Service Worker 缓存治理
- reload、多进程、多实例发布策略
- Node、包管理器、环境变量统一治理
- CI/CD、E2E 与真实用户验证链路
- build/version 暴露与健康检查
- design token 与主题资源一致性治理
- 灰度、回滚、旧资源清理机制
- Runtime no-progress safety stop 防回归

---

# 2. 总体架构原则

## 2.1 Immutable Artifact（不可变构建产物）

生产环境禁止直接覆盖运行目录。

所有部署必须基于：

- 独立构建产物
- 独立 release 目录
- 原子切换
- 可追溯 build id

推荐结构：

```text
/releases
  /2026-09-21-001
  /2026-09-21-002
current -> /releases/2026-09-21-002
```

禁止：

- 手工覆盖 dist
- 直接 rsync 到运行目录
- 线上热修改 CSS
- reload 后继续复用旧资源目录

## 2.2 单一真实版本源（Single Source of Truth）

所有运行时版本必须来自统一 build metadata。

统一生成：

```json
{
  "build_id": "2026.09.21-001",
  "git_commit": "a13f9d",
  "build_time": "2026-09-21T15:30:00Z",
  "node_version": "20.18.0",
  "package_manager": "pnpm@9.12.1"
}
```

metadata 必须同时注入：

- HTML meta
- HTTP response header
- /health/build endpoint
- 前端 runtime telemetry
- E2E 截图与日志

---

# 3. 静态资源 Hash 与 Version 管理

## 3.1 构建产物命名规范

所有静态资源必须采用 content hash。

禁止：

```text
/static/app.css
/static/main.js
```

必须：

```text
/static/app.a13f9d.css
/static/main.84cc71.js
/static/vendor.4ea001.js
```

要求：

- 文件内容变化必须导致 hash 变化
- hash 长度建议 8~16 位
- chunk、字体、图片、theme asset 全部版本化
- manifest 自动生成

推荐：

```text
/dist
  index.html
  asset-manifest.json
  assets/
```

## 3.2 Manifest 管理

HTML 不允许手写静态资源名称。

必须通过 manifest 自动注入：

```json
{
  "app.css": "app.a13f9d.css",
  "main.js": "main.84cc71.js"
}
```

部署时校验：

- manifest 与 dist 文件一致
- HTML 中引用的文件真实存在
- 无 orphan chunk
- 无重复 hash

CI 中增加：

```bash
verify-manifest-integrity
```

## 3.3 旧资源保留策略

生产环境禁止部署完成后立即删除旧资源。

建议：

- 保留最近 7~14 天资源
- 或保留最近 N 个 release

原因：

- 用户浏览器可能仍持有旧 HTML
- Service Worker 可能延迟更新
- 灰度用户可能仍访问旧版本
- 回滚需要旧资源恢复能力

建议：

```text
/assets-retention-policy
  keep_last_releases = 5
  keep_days = 14
```

## 3.4 旧资源淘汰机制

旧资源清理必须满足：

- 当前线上无实例引用
- CDN 已完成新版本传播
- 灰度已结束
- 无活跃 Service Worker 使用旧 cache
- rollback window 已过期

推荐异步 GC 流程：

```text
build complete
-> deploy complete
-> verify complete
-> wait retention window
-> CDN reference check
-> storage cleanup
```

---

# 4. HTML、CDN 与浏览器缓存治理

## 4.1 HTML 缓存策略

HTML 是静态资源入口。

HTML 必须短缓存或禁缓存：

```http
Cache-Control: no-store, no-cache, must-revalidate
```

目标：

- 用户始终获取最新 HTML
- HTML 始终引用最新 hash 资源

禁止：

```http
Cache-Control: max-age=86400
```

否则用户会长期持有旧 HTML。

## 4.2 Hash 静态资源缓存策略

Hash 资源必须长缓存：

```http
Cache-Control: public, max-age=31536000, immutable
```

收益：

- 浏览器无需重复下载
- CDN 命中率提升
- 更新不会污染旧资源

## 4.3 CDN 治理策略

CDN 必须：

- cache key 包含完整 path
- 不忽略 hash 文件名
- HTML 与静态资源使用不同缓存规则
- 发布后自动 refresh HTML
- 支持灰度路由

禁止：

```text
style.css?v=123
```

原因：

- 多级 CDN 行为不一致
- query cache 不稳定
- 回滚困难

## 4.4 浏览器缓存治理

前端启动后必须主动检测：

- 当前 HTML build id
- CSS hash
- JS hash
- API 返回 build id

若检测到：

- HTML 与 JS 版本不一致
- API 与前端版本不一致

则触发：

- toast 提示
- 自动 refresh
- 灰度 fallback

## 4.5 Service Worker 策略

若项目启用 Service Worker，必须显式治理版本生命周期。

禁止：

- 自动永久缓存 HTML
- 激活后继续引用旧 cache

推荐：

```text
HTML -> network first
hash assets -> cache first
API -> stale while revalidate
```

Service Worker 更新流程：

```text
new SW downloaded
-> waiting
-> user notified
-> old tabs closed
-> activate new SW
-> cleanup old caches
```

缓存命名必须带 build id：

```text
app-cache-2026-09-21-001
```

旧 cache 清理：

```js
caches.keys()
```

保留：

- 当前版本
- rollback 版本

其余自动删除。

---

# 5. Reload、多进程与滚动发布策略

## 5.1 生产环境禁止 Dev Reload

禁止：

- watch mode
- hot reload serving production traffic
- 开发 reload worker 进入生产

生产环境必须使用：

```text
immutable artifact + rolling restart
```

## 5.2 多进程版本一致性

所有 worker 必须运行相同 build。

启动时校验：

```text
BUILD_ID
ASSET_MANIFEST_HASH
NODE_VERSION
```

发现版本不一致立即拒绝加入流量池。

## 5.3 滚动发布策略

推荐：

```text
旧实例 draining
-> 新实例 ready
-> readiness pass
-> E2E smoke pass
-> 切流
-> 旧实例退出
```

旧实例必须：

- 停止接收新请求
- 完成已有请求
- 释放旧资源引用

## 5.4 Reload 与旧进程治理

部署后自动检测：

```bash
lsof -i :8080
ps -ef
```

检查：

- 是否存在旧 PID
- 是否存在 zombie worker
- 是否存在旧 release 路径引用

若存在旧进程：

- 自动阻断 rollout
- 自动告警
- 自动 terminate

## 5.5 多实例灰度一致性

灰度期间必须保证：

- 同一 session 固定命中同一 build
- CDN 与应用实例版本一致
- HTML 与 API build id 一致

建议：

```text
sticky session
or
build-aware routing
```

---

# 6. 统一运行环境治理

## 6.1 Node 与包管理器统一

统一版本：

```text
Node 20.x
pnpm 9.x
```

必须提交：

- .nvmrc
- packageManager 字段
- lockfile

CI 中强制校验：

```bash
node --version
pnpm --version
```

版本不一致直接失败。

## 6.2 Python 与 Runtime 环境统一

若包含 Python Runtime：

统一使用：

```bash
./.venv/bin/python
```

禁止：

```bash
python
python3
```

原因：

- 系统解释器漂移
- pytest 不一致
- reload 子进程环境不同

## 6.3 环境变量治理

所有环境变量必须 schema 化。

例如：

```text
APP_ENV
CDN_BASE_URL
BUILD_ID
FEATURE_FLAGS
```

启动前校验：

- 缺失变量
- 非法值
- 类型错误

不满足直接 fail fast。

## 6.4 Hermetic Build

推荐：

- Docker build
- 固定基础镜像
- 固定 timezone
- 固定 locale

避免：

- 本地打包上传
- 开发机构建生产资源

---

# 7. CI/CD 构建与验证体系

## 7.1 CI Pipeline

推荐阶段：

```text
lint
-> typecheck
-> unit test
-> build
-> manifest verify
-> bundle verify
-> E2E
-> visual regression
-> security scan
-> artifact publish
```

## 7.2 构建阶段校验

必须验证：

- dist 完整性
- manifest 正确性
- hash 文件存在
- source map 正确生成
- build metadata 注入成功

CI 中自动执行：

```bash
verify-dist-integrity
```

## 7.3 E2E 验证

E2E 必须从真实用户视角验证。

推荐工具：

- Playwright
- Cypress
- Puppeteer

验证项：

- 页面可打开
- CSS 已加载
- computed style 正确
- design token 生效
- hydration 正常
- chunk 加载成功
- dark/light theme 正常
- 多浏览器兼容

## 7.4 Visual Regression

必须保存 screenshot baseline。

变更后自动对比：

- spacing
- typography
- color
- radius
- token
- layout

差异超阈值自动阻断部署。

## 7.5 Post Deploy Verification

部署后自动执行：

```text
访问首页
-> 检查 build id
-> 检查 CSS hash
-> 检查 JS hash
-> 检查 health endpoint
-> 检查 screenshot
-> 检查核心交互
```

失败则：

- 自动 rollback
- 自动停止 rollout

---

# 8. Runtime Build/Version 健康检查

## 8.1 健康检查接口

必须提供：

```text
/health
/health/build
/version
```

返回：

```json
{
  "status": "ok",
  "build_id": "2026.09.21-001",
  "git_commit": "a13f9d",
  "release": "release-2026-09-21-001"
}
```

## 8.2 Response Header 暴露

所有页面与 API 必须包含：

```http
X-App-Version
X-Build-Id
X-Release-Id
```

用于：

- 浏览器验证
- CDN 排查
- 灰度定位
- 用户反馈追踪

## 8.3 前端 Runtime Version 校验

前端启动后：

```text
读取 HTML build id
-> 校验 asset manifest
-> 校验 API build id
-> 校验 service worker version
```

若不一致：

- 上报 telemetry
- 提示用户刷新
- 自动恢复

## 8.4 Runtime Telemetry

监控指标：

- chunk load fail
- CSS load fail
- hydration mismatch
- build mismatch
- stale cache hit
- rollback rate
- old worker detected

---

# 9. Design Token 与样式一致性治理

## 9.1 单一 Token Source

禁止：

- 页面私有 token
- inline style patch
- 多套主题变量并存

统一：

```css
--color-primary
--bg-container
--radius-md
--shadow-sm
```

## 9.2 Token 发布流程

token 更新必须：

```text
design review
-> token diff
-> screenshot verify
-> E2E verify
-> rollout
```

## 9.3 Token 校验

CI 自动检查：

- 未注册 token
- 重复 token
- dark/light 冲突
- 未使用 token
- 非法颜色

## 9.4 样式一致性基线

建立统一基线：

- typography scale
- spacing scale
- radius system
- shadow system
- color semantic

禁止业务页面绕过 Design System。

## 9.5 Design Token 回滚

token 发布必须可回滚。

要求：

- token bundle version 化
- 历史 token retained
- rollback 后可恢复旧主题

---

# 10. 灰度、回滚与异常恢复机制

## 10.1 灰度发布

建议：

```text
1% -> 5% -> 20% -> 50% -> 100%
```

监控：

- CSS load error
- JS exception
- FCP/LCP
- hydration mismatch
- chunk load fail
- 用户投诉率

## 10.2 自动回滚条件

满足以下条件自动 rollback：

- health check fail
- build mismatch
- visual regression fail
- error rate 激增
- chunk load fail 激增

## 10.3 回滚原则

回滚必须整体回滚：

- HTML
- manifest
- CSS
- JS
- Service Worker
- config
- token bundle

禁止只回滚部分资源。

## 10.4 异常恢复

必须具备：

- CDN fallback
- 静态资源镜像
- rollback manifest
- release snapshot

恢复流程：

```text
detect issue
-> freeze rollout
-> route fallback
-> restore release
-> verify build
-> reopen traffic
```

---

# 11. 真实用户视角的 UI 生效验证

## 11.1 真实页面验证

禁止只验证：

- unit test
- lint
- build success

必须验证：

- 浏览器真实渲染
- 样式真实加载
- 页面 hierarchy 正确
- interaction 正常

## 11.2 Browser Automation

自动执行：

```text
open page
-> wait hydration
-> collect screenshot
-> collect computed style
-> verify build id
```

## 11.3 Computed Style 验证

验证关键样式：

```js
getComputedStyle(element)
```

检查：

- font-size
- color
- spacing
- border-radius
- display
- theme token

## 11.4 多维度验证

验证维度：

- Chrome
- Safari
- Firefox
- Desktop
- Mobile
- 多地域 CDN
- 灰度用户

## 11.5 人工验收 Checklist

上线后人工确认：

- 页面无旧样式残留
- 无双层容器
- token 生效一致
- dark/light 正确
- build/version 正确
- 刷新后样式稳定
- 登录态不受影响

---

# 12. Runtime No-Progress Safety Stop 专项治理

## 12.1 Verification 必须标准化

mutation 后固定流程：

```text
apply patch
-> build
-> test
-> runtime verify
-> collect evidence
-> complete
```

禁止：

- 修改后不验证
- 只输出描述
- 无真实 evidence

## 12.2 验证命令统一

统一使用：

```bash
./.venv/bin/python -m pytest tests -q
```

或：

```bash
python scripts/run_in_project_env.py pytest tests -q
```

禁止：

```bash
python3 -m pytest
```

## 12.3 Runtime Verification Evidence

verification evidence 必须包含：

- exit code
- build id
- artifact hash
- screenshot
- health result
- runtime version

## 12.4 防止旧进程误验证

验证前必须：

```bash
lsof -i :8080
```

检查：

- 是否仍是旧 PID
- 是否仍引用旧 release

若存在旧进程：

- 阻断 verification
- 强制重启

## 12.5 Verification 链路健康监控

监控：

- verification timeout
- command spawn failure
- stale runtime
- no-progress retry loop

连续失败直接阻断发布。

---

# 13. 推荐落地目录结构

```text
/docs
  production-deployment-verification-architecture.md

/releases
/dist
/assets
/manifests
/scripts
/tests/e2e
/tests/visual
```

---

# 14. 推荐发布流程（标准版）

```text
代码提交
-> CI lint/typecheck/unit test
-> build immutable artifact
-> manifest verify
-> E2E verify
-> visual regression
-> publish artifact
-> deploy canary
-> runtime health verify
-> browser verification
-> gradual rollout
-> telemetry observe
-> full rollout
-> retention cleanup
```

---

# 15. 最终落地结论

生产级 UI 部署与验证体系的核心，不是单纯“CSS 修改”，而是建立一整套：

- immutable artifact
- hash/version 资源体系
- HTML/CDN/浏览器缓存治理
- 多进程与滚动发布一致性
- 环境统一与 hermetic build
- E2E + visual regression
- runtime build/version health
- design token governance
- 灰度与自动回滚
- 真实用户视角验证
- Runtime verification evidence

只有这些链路同时成立，才能真正避免：

- 本地与线上 UI 不一致
- 用户命中旧资源
- reload 后样式不更新
- CDN 缓存污染
- rollback 白屏
- no-progress safety stop
- 多实例版本混杂
- “明明改了但线上没生效”

并确保多人使用服务场景下的 UI 修改能够稳定、可验证、可回滚、可观测地真实生效。
