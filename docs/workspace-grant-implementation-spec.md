# Workspace Grant 外部目录访问实施 Spec

## 1. 目标

在不改变现有 Conversation Loop、主 Workspace、Tool Policy、Approval、Checkpoint 流程的前提下，为 tsm-agent 增加受控的外部目录读写能力。

目标场景：

```text
用户：在 /Users/tusima/Documents/学习/dashboard-demo 创建新工程
模型：申请外部目录访问
系统：暂停当前 Turn，等待用户授权
用户：允许
系统：恢复原 Turn，继续执行工具调用
```

必须满足：

- 默认行为不变；
- 未授权外部路径仍然拒绝；
- 授权范围绑定 Session / Task；
- 读权限和写权限分离；
- 授权后能够恢复原 Turn；
- 重启后授权状态可恢复；
- 不新增 Agent 类型；
- 不改变 Conversation Loop；
- 不绕过现有 Tool Policy 和 Approval。

## 2. 非目标

本期不实现：

- 任意路径永久授权；
- 全局目录白名单；
- 自动信任整个用户 Home；
- OS 级沙箱替代；
- 绕过命令审批；
- 自动给模型授予外部目录权限；
- 跨 Session 默认继承写权限。

## 3. 核心概念

### 3.1 主 Workspace

Task 创建时已有的工作区 `task.workspace`。主 Workspace 行为保持不变。

### 3.2 附加 Workspace Grant

外部目录访问授权，挂在 Task 上：

```text
Task
 ├── primary workspace
 └── workspace grants
      ├── external read grant
      └── external write grant
```

Grant 不是新的 Workspace，也不改变 Task 的主 workspace。

## 4. 数据模型

新增 `src/tsm_agt/ports/workspace_grant.py`。

```python
class WorkspaceGrantAccess(StrEnum):
    READ = "READ"
    WRITE = "WRITE"


class WorkspaceGrantStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    DENIED = "DENIED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class WorkspaceGrantTrust(StrEnum):
    READ_ONLY = "READ_ONLY"
    TRUSTED_BUILD = "TRUSTED_BUILD"
    TRUSTED_FULL = "TRUSTED_FULL"
```

```python
@dataclass(frozen=True, slots=True)
class WorkspaceGrant:
    grant_id: str
    session_id: str
    task_id: str
    root_path: str
    canonical_path: str
    access: WorkspaceGrantAccess
    trust: WorkspaceGrantTrust
    status: WorkspaceGrantStatus
    reason: str
    requested_by_turn_id: str
    approved_by: str | None
    created_at: str
    updated_at: str
    expires_at: str | None
```

## 5. 持久化

新增 SQLite 表：

```sql
CREATE TABLE workspace_grants (
    grant_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    access TEXT NOT NULL,
    trust TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_by_turn_id TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    UNIQUE(task_id, canonical_path, access)
);
```

索引：

```sql
CREATE INDEX idx_workspace_grants_task
ON workspace_grants(task_id, status);

CREATE INDEX idx_workspace_grants_session
ON workspace_grants(session_id, status);
```

旧数据库启动时自动 migration。

## 6. 路径判断

新增 `src/tsm_agt/core/workspace_grants.py`，统一负责路径 canonicalization、Grant 查询和状态变更。

```python
class WorkspaceGrantService:
    async def request(...): ...
    async def approve(...): ...
    async def deny(...): ...
    async def revoke(...): ...
    async def resolve(...): ...
```

路径解析顺序：

```text
requested path
    ↓ expanduser
    ↓ absolute
    ↓ resolve(strict=False)
    ↓ canonical path
```

必须拒绝：

- 空路径；
- 相对路径；
- `/`；
- 用户 Home 根目录；
- 系统目录；
- `.ssh`、`.aws`、钥匙串等敏感目录；
- 目录穿越；
- symlink escape。

匹配规则：

```text
主 workspace 内
    → 沿用现有规则

外部目录在 ACTIVE READ grant 内
    → 允许读

外部目录在 ACTIVE WRITE grant 内
    → 允许读写

只有 READ grant 但请求写入
    → WORKSPACE_ACCESS_REQUIRED

没有 grant
    → WORKSPACE_ACCESS_REQUIRED
```

子目录可以继承父 Grant，父目录不能被子目录 Grant 反向覆盖。

## 7. 新工具：workspace.request_access

工具 schema 必须保持普通 object 顶层，不能使用顶层 `oneOf`、`anyOf`、`allOf`、`enum`、`const`，以兼容 OpenAI-compatible Provider。

```json
{
  "name": "workspace.request_access",
  "parameters": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "access": {"type": "string"},
      "trust": {"type": "string"},
      "reason": {"type": "string"}
    },
    "required": ["path", "access", "reason"],
    "additionalProperties": false
  }
}
```

申请成功返回 `PENDING_APPROVAL`，已有 ACTIVE Grant 时返回 `already_granted: true`。

## 8. 工具策略

新增错误码：

```text
WORKSPACE_ACCESS_REQUIRED
WORKSPACE_GRANT_PENDING
WORKSPACE_GRANT_DENIED
WORKSPACE_GRANT_EXPIRED
WORKSPACE_GRANT_REVOKED
INVALID_EXTERNAL_PATH
```

未授权外部路径不得只返回普通 `PERMISSION_DENIED`，应返回：

```json
{
  "ok": false,
  "error_code": "WORKSPACE_ACCESS_REQUIRED",
  "path": "...",
  "required_access": "WRITE",
  "recovery": {
    "kind": "REQUEST_WORKSPACE_ACCESS",
    "suggested_tool": "workspace.request_access"
  }
}
```

用户拒绝后，当前 Turn 返回模型，不自动重复申请同一路径。

## 9. Turn 状态机

```text
RUNNING
  ↓
WORKSPACE_ACCESS_REQUIRED
  ↓
AWAITING_USER
  ├── APPROVE → GRANT_ACTIVE → RESUME_TURN
  ├── DENY    → TOOL_FAILED → MODEL_CONTINUES
  └── CANCEL  → TURN_CANCELLED
```

新增 `PendingUserActionKind.WORKSPACE_ACCESS`。它复用 checkpoint/resume 基础设施，但不混入普通工具风险语义。

## 10. Checkpoint

`AgentTurnCheckpoint` 新增 `workspace_grant_request`：

```json
{
  "grant_id": "grant-...",
  "path": "/Users/tusima/Documents/学习/dashboard-demo",
  "access": "WRITE",
  "trust": "TRUSTED_BUILD",
  "reason": "创建新的 Dashboard 工程",
  "status": "PENDING"
}
```

恢复时重新验证 Grant 状态、canonical path、access、trust 和 symlink 安全性。

## 11. 事件

新增：

```text
workspace_grant.requested
workspace_grant.approved
workspace_grant.denied
workspace_grant.revoked
workspace_grant.expired
workspace_grant.reused
workspace_grant.path_rejected
```

事件中禁止记录 token、密钥、文件内容和敏感命令参数。

## 12. Web API 与 SSE

```http
GET  /tasks/{task_id}/workspace-grants
POST /workspace-grants/{grant_id}/approve
POST /workspace-grants/{grant_id}/deny
```

SSE waiting payload：

```json
{
  "waiting": {
    "kind": "WORKSPACE_ACCESS",
    "grant_id": "grant-...",
    "path": "...",
    "access": "WRITE",
    "trust": "TRUSTED_BUILD",
    "reason": "..."
  }
}
```

前端显示外部目录授权卡片。批准后清除 waiting、保留 Task card、重连 SSE、恢复原 Turn。

## 13. 工具接入范围

第一阶段接入：

```text
core.run_command
core.read_file
core.list_files
core.search_files
core.search_text
core.apply_patch
core.delete_file
```

所有工具统一调用 Grant Service，不在工具内部重复实现路径安全逻辑。

## 14. 安全规则

默认配置：

```python
external_workspace_access = "approval_required"
```

WRITE Grant 不自动授予网络访问；`TRUSTED_BUILD` 不等于 `TRUSTED_FULL`；Grant 不跨 Session 自动继承。

## 15. 实施阶段

### Phase 1：数据与核心服务

- 新增 Grant 数据模型；
- 新增 SQLite migration；
- 新增路径 canonicalization；
- 新增申请、批准、拒绝、撤销、查询；
- 新增 Grant 事件。

### Phase 2：工具策略

- 接入只读工具；
- 增加 `WORKSPACE_ACCESS_REQUIRED`；
- 保持主 Workspace 行为不变。

### Phase 3：写入和命令

- 接入 apply_patch、delete_file、run_command；
- 增加 WRITE / TRUSTED_BUILD 审批。

### Phase 4：Turn 恢复

- checkpoint 保存 Grant request；
- SSE 发布 waiting；
- Web approve/deny；
- 原 Turn 恢复；
- 重启恢复。

### Phase 5：前端

- Workspace Access 卡片；
- 允许/拒绝按钮；
- SSE 状态同步；
- 授权后恢复实时轨迹。

## 16. 验收标准

### AC-1 默认行为

未授权外部目录不得执行，返回 `WORKSPACE_ACCESS_REQUIRED`。

### AC-2 读授权

READ Grant 允许 list/read/search，但不能写。

### AC-3 写授权

WRITE Grant 允许 apply_patch/delete_file。

### AC-4 命令授权

TRUSTED_BUILD Grant 允许受策略约束的 `core.run_command`。

### AC-5 拒绝恢复

拒绝后不重复弹窗，模型收到 `WORKSPACE_GRANT_DENIED`。

### AC-6 Turn 恢复

批准后产生 `workspace_grant.approved`、`turn.resumed`，原工具调用继续执行。

### AC-7 重启

ACTIVE、PENDING、DENIED Grant 在 SQLite 重启后状态正确恢复。

### AC-8 路径安全

根目录、系统目录、敏感目录、目录穿越和 symlink escape 全部拒绝。

### AC-9 主路径回归

Conversation Loop、Turn Finalization、Checkpoint、Approval、SSE、Workspace Path 测试全部通过。

## 17. 用户体验示例

```text
用户：在 /Users/tusima/Documents/学习/dashboard-demo 创建一个 FastAPI 工程

模型：workspace.request_access(
  path="/Users/tusima/Documents/学习/dashboard-demo",
  access="WRITE",
  trust="TRUSTED_BUILD",
  reason="创建新工程"
)

UI：请求访问外部目录
    [允许] [拒绝]

用户点击允许

系统：workspace_grant.approved → turn.resumed

模型继续：core.run_command → core.apply_patch → core.run_command
```
