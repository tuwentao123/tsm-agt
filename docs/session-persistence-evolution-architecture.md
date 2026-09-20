# 会话持久化与恢复演进式架构方案

## 1. 目标与设计原则

本文定义一个可演进的“会话持久化与恢复”方案：

- 当前阶段：仅依赖前端本地存储实现 MVP，无需真实后端部署。
- 未来阶段：可平滑迁移到服务端 Session / Message 持久化架构。
- 长期阶段：支持多设备同步、离线恢复、消息同步与扩展字段演进。

核心原则：

- 前后端职责解耦。
- 数据结构版本化与可扩展。
- 本地优先（Local First）保证离线可用。
- 服务端未来作为最终状态源（Source of Truth）。
- 当前 MVP 不要求真实服务端同步能力。

---

## 2. 架构演进路线

### 阶段 1：前端本地持久化 MVP（当前阶段）

目标：

- 浏览器刷新后恢复会话。
- 前端重启后恢复消息历史。
- 无需后端数据库。
- 支持离线使用。

架构：

```text
Browser UI
   │
   ├── SessionStore
   ├── MessageStore
   └── Local Persistence Layer
          ├── localStorage（索引与元信息）
          └── IndexedDB（消息正文）
```

特点：

- Session 与 Message 全部保存在浏览器。
- 页面刷新后自动恢复。
- 当前不依赖服务端。
- 接口协议提前抽象，避免未来重构 UI。

---

### 阶段 2：引入服务端持久化（未来阶段）

目标：

- 支持跨设备同步。
- 支持登录用户会话。
- 支持服务端长期存储。
- 支持多实例部署。

架构：

```text
Frontend App
   │
   ├── Local Cache Layer
   │       ├── localStorage
   │       └── IndexedDB
   │
   └── Sync Service
            │
            ▼
      Session API Gateway
            │
   ┌────────┴────────┐
   │                 │
Session Service   Message Service
   │                 │
   └────────┬────────┘
            ▼
        PostgreSQL
            │
            ▼
          Redis
```

职责边界：

前端：

- 本地缓存。
- 离线恢复。
- 同步状态管理。
- 冲突检测与重试。

服务端：

- Session 生命周期管理。
- Message 永久存储。
- 用户身份绑定。
- 多端同步。
- 数据版本管理。

---

## 3. 当前 MVP 本地持久化设计

## 3.1 本地存储分层

### localStorage

用于保存轻量级索引信息：

```json
{
  "activeSessionId": "sess_001",
  "sessionOrder": ["sess_001", "sess_002"],
  "storageVersion": 1
}
```

存储内容：

- 当前激活 Session。
- Session 排序。
- 数据版本号。
- 最近访问时间。

特点：

- 读取快。
- 适合启动恢复。
- 不保存大量消息正文。

---

### IndexedDB

用于保存完整 Session 与 Message 数据。

推荐 Object Store：

```text
sessions
messages
sync_queue
metadata
```

Session 数据示例：

```json
{
  "id": "sess_001",
  "title": "新会话",
  "createdAt": "2026-09-20T10:00:00Z",
  "updatedAt": "2026-09-20T10:20:00Z",
  "messageCount": 12,
  "localOnly": true,
  "syncStatus": "local",
  "schemaVersion": 1
}
```

Message 数据示例：

```json
{
  "id": "msg_001",
  "sessionId": "sess_001",
  "role": "user",
  "content": "你好",
  "createdAt": "2026-09-20T10:00:01Z",
  "clientSequence": 1,
  "syncStatus": "pending",
  "schemaVersion": 1,
  "ext": {}
}
```

---

## 3.2 会话恢复流程

应用启动：

```text
App Start
   ↓
读取 localStorage.activeSessionId
   ↓
从 IndexedDB 加载 Session
   ↓
读取 Message 列表
   ↓
恢复 UI 状态
```

消息发送：

```text
用户发送消息
   ↓
先写入 IndexedDB
   ↓
更新 Session updatedAt
   ↓
更新 UI
   ↓
（未来可扩展）进入同步队列
```

删除会话：

```text
删除 Session
   ↓
删除关联 Message
   ↓
更新 localStorage 索引
```

---

## 3.3 生命周期管理

Session 生命周期：

```text
CREATED
   ↓
ACTIVE
   ↓
ARCHIVED
   ↓
DELETED
```

Message 生命周期：

```text
PENDING
   ↓
STORED_LOCAL
   ↓
SYNC_PENDING（未来）
   ↓
SYNCED（未来）
```

本地清理策略：

- 自动清理超过 TTL 的临时会话。
- 保留用户固定收藏会话。
- IndexedDB 超限时进行 LRU 清理。

---

## 4. 未来服务端架构设计

## 4.1 服务拆分

### Session Service

职责：

- Session 创建。
- Session 查询。
- 会话归档。
- 会话权限控制。
- Session 元信息维护。

### Message Service

职责：

- Message 持久化。
- Message 分页查询。
- Message 增量同步。
- Token / Metadata 扩展。

### Sync Service

职责：

- 本地与服务端状态同步。
- 冲突检测。
- 重试机制。
- 增量同步。

---

## 4.2 服务端数据模型

### session 表

```sql
CREATE TABLE sessions (
  id VARCHAR(64) PRIMARY KEY,
  user_id VARCHAR(64),
  title VARCHAR(255),
  latest_message_id VARCHAR(64),
  message_count INT DEFAULT 0,
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  archived_at TIMESTAMP NULL,
  schema_version INT DEFAULT 1,
  ext JSONB
);
```

### message 表

```sql
CREATE TABLE messages (
  id VARCHAR(64) PRIMARY KEY,
  session_id VARCHAR(64),
  role VARCHAR(16),
  content TEXT,
  created_at TIMESTAMP,
  token_count INT,
  client_sequence BIGINT,
  server_sequence BIGINT,
  sync_version BIGINT,
  deleted BOOLEAN DEFAULT FALSE,
  schema_version INT DEFAULT 1,
  ext JSONB
);
```

扩展字段：

- ext：用于未来 metadata 扩展。
- schema_version：用于结构升级。
- sync_version：用于增量同步。

---

## 5. 接口协议设计

## 5.1 Session 接口

### 创建 Session

```http
POST /api/v1/sessions
```

请求：

```json
{
  "title": "新会话"
}
```

响应：

```json
{
  "session": {
    "id": "sess_001",
    "syncVersion": 1
  }
}
```

---

### 获取 Session 列表

```http
GET /api/v1/sessions
```

支持：

- 分页
- 最近更新时间排序
- 增量同步

---

### 更新 Session

```http
PATCH /api/v1/sessions/{sessionId}
```

请求：

```json
{
  "title": "重命名会话",
  "archived": false
}
```

---

## 5.2 Message 接口

### 创建 Message

```http
POST /api/v1/sessions/{sessionId}/messages
```

请求：

```json
{
  "id": "msg_001",
  "role": "user",
  "content": "你好",
  "clientSequence": 1
}
```

响应：

```json
{
  "message": {
    "id": "msg_001",
    "serverSequence": 1001,
    "syncVersion": 20
  }
}
```

---

### 查询 Message 历史

```http
GET /api/v1/sessions/{sessionId}/messages
```

支持：

- cursor 分页。
- 增量拉取。
- 时间范围过滤。

---

### 批量同步 Message

```http
POST /api/v1/sync/messages
```

请求：

```json
{
  "clientId": "web_001",
  "lastSyncVersion": 10,
  "messages": []
}
```

响应：

```json
{
  "ackVersion": 20,
  "conflicts": [],
  "serverChanges": []
}
```

---

## 6. 本地与服务端同步机制

## 6.1 同步状态设计

Message syncStatus：

```text
local
pending
syncing
synced
failed
conflict
```

Session syncStatus：

```text
local
partial
synced
```

---

## 6.2 同步时机

触发条件：

- 用户发送消息。
- 页面重新连接网络。
- 用户主动刷新。
- 应用后台定时同步。
- 页面重新打开。

同步流程：

```text
读取 sync_queue
   ↓
上传 pending messages
   ↓
服务端返回 ackVersion
   ↓
拉取增量变更
   ↓
更新本地 IndexedDB
```

---

## 6.3 冲突处理策略

推荐采用：

- Message：Append-Only。
- Session Metadata：Last Write Wins。

冲突场景：

- 多设备同时修改标题。
- 本地离线期间新增消息。
- 服务端消息已删除。

处理原则：

- Message 不覆盖正文。
- 元数据按更新时间合并。
- 保留冲突日志。
- 支持人工恢复。

---

## 6.4 失败恢复

失败场景：

- 网络中断。
- 服务端超时。
- Token 失效。
- 数据结构版本不兼容。

恢复策略：

- sync_queue 本地持久化。
- 指数退避重试。
- 增量同步恢复。
- schemaVersion 不兼容时触发迁移。

---

## 7. 平滑迁移方案

## 7.1 MVP 到服务端迁移路径

阶段 1：

```text
仅本地 IndexedDB
```

阶段 2：

```text
本地 IndexedDB + 可选云同步
```

阶段 3：

```text
服务端成为主存储
本地作为缓存层
```

---

## 7.2 兼容性策略

关键原则：

- UI 不直接依赖存储实现。
- Repository 接口保持稳定。
- Session / Message Schema 保持兼容。

推荐抽象：

```text
ChatRepository
   ├── LocalRepository
   └── RemoteRepository
```

前端仅依赖：

```ts
interface ChatRepository {
  getSession(id)
  saveMessage(message)
  listMessages(sessionId)
  sync()
}
```

这样可以在不改 UI 的情况下切换实现。

---

## 7.3 数据迁移策略

首次登录服务端时：

```text
读取本地 Session
   ↓
上传至服务端
   ↓
服务端建立映射关系
   ↓
返回 serverSessionId
   ↓
更新本地 syncStatus
```

迁移期间：

- 本地 ID 保留。
- 服务端生成 canonicalId。
- 建立 localId → serverId 映射。

避免问题：

- UI 会话丢失。
- Message 顺序错乱。
- 重复同步。

---

## 8. 版本化与扩展策略

## 8.1 Schema Version

所有实体必须包含：

```json
{
  "schemaVersion": 1
}
```

用于：

- IndexedDB 升级。
- 服务端兼容。
- 增量迁移。

---

## 8.2 字段扩展策略

新增字段原则：

- 新字段必须 optional。
- 不删除旧字段。
- 保持向后兼容。

推荐扩展字段：

```json
{
  "ext": {
    "model": "gpt-5.5",
    "temperature": 0.7,
    "attachments": []
  }
}
```

---

## 8.3 IndexedDB 升级策略

```text
v1:
  sessions + messages

v2:
  sync_queue

v3:
  attachment metadata
```

升级要求：

- 保证数据迁移幂等。
- 升级失败可回滚。
- 保留旧版本读取能力。

---

## 9. 最终建议

当前 MVP 建议：

- localStorage 保存 Session 索引。
- IndexedDB 保存完整 Message。
- UI 使用 Repository 抽象。
- 数据结构提前包含 syncStatus / schemaVersion。
- 本地先实现 append-only message 模型。

未来服务端阶段建议：

- PostgreSQL 作为最终持久层。
- Redis 作为热点缓存。
- Session / Message 服务解耦。
- 使用增量同步与版本号机制。
- 本地缓存继续保留，支持离线恢复。

最终形成：

```text
Local First + Cloud Sync + Versioned Schema
```

既能快速完成当前 MVP，又能在后续平滑演进为可扩展、多设备、可恢复的正式服务端架构。
