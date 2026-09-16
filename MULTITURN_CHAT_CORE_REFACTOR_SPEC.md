# Multi-turn Chat Core Refactor SPEC

## 1. 目标

建立一个“始终可继续对话”的稳定多轮会话主链路。

当前系统的问题不是缺少 Session、Task、Runtime 或 Store，
而是这些模块同时参与了普通聊天主流程，导致：

- 普通追问依赖 runtime routing
- outcome gating 阻断自然聊天
- active task 自动恢复污染新输入
- protocol failure 会中断会话
- runtime state 与 chat state 强耦合

本次重构目标：

1. 普通聊天永远优先成功
2. Runtime 只负责副作用边界
3. Session 与 Task 解耦
4. Resume 变成窄条件行为
5. Outcome 从 gate 改为 observability
6. 建立最小稳定 chat core

---

## 2. 当前结构问题

### 2.1 session 与 runtime 强耦合

当前 `runtime_input.py` 同时负责：

- continuation classification
- task relation
- semantic routing
- resume decision
- clarification
- follow-up derivation

导致普通消息：

```text
用户输入
→ runtime route
→ relation inference
→ continuation decision
→ outcome compatibility
→ runtime steering
→ 主模型
```

而不是：

```text
用户输入
→ session context
→ 主模型
```

### 2.2 active task 生命周期过宽

当前系统倾向于：

```text
存在 active task
=> 默认恢复 task
```

这会导致：

- 新问题被旧 task 吸附
- 已完成 task 被反复恢复
- outcome graph 无限增长
- session 无法自然切换上下文

### 2.3 Outcome 参与主流程控制

当前 outcome 被用于：

- tool call eligibility
- routing gate
- execution validation
- conversation continuation

导致：

```text
invalid_outcome_binding
unknown_outcome
```

直接中断普通会话。

---

## 3. 新架构目标

建立双层结构：

```text
Flexible Conversation Layer
    ↓
Deterministic Execution Layer
```

其中：

### Chat Layer 负责

- 多轮消息历史
- session context
- task summary
- artifact references
- 普通自然语言追问
- 主模型 reasoning
- tool planning

### Runtime Layer 只负责

- 文件修改
- 命令执行
- approval
- sandbox
- mutation journal
- checkpoint compatibility
- deterministic replay

Runtime 不再决定：

- 用户是不是 continuation
- 是否允许普通聊天继续
- follow-up 属于哪种语义关系

---

## 4. 最小稳定主链路

目标链路：

```text
用户输入
→ Session Manager
→ Context Assembler
→ Main Model
→ Tool Loop
→ Save Messages
→ 返回用户
```

严格禁止以下模块阻断聊天：

- outcome graph
- semantic router
- runtime verifier
- workflow orchestrator
- replay validator

这些模块只能参与：

```text
副作用执行阶段
```

而不是：

```text
普通聊天阶段
```

---

## 5. Task 生命周期重构

### 5.1 新状态模型

仅保留：

```text
RUNNING
WAITING_USER
INTERRUPTED
COMPLETED
```

### 5.2 Resume 必须由持久化状态决定

禁止：

```python
if active_task:
    resume()
```

普通文本不参与 Runtime 容器选择，改为：

```python
should_resume = (
    task.state in [INTERRUPTED, CONFLICT]
    or (
        task.state == WAITING_USER
        and task.pending_user_action.kind == CONTINUATION
    )
)
```

### 5.3 禁止自然语言 continuation 词表

不得通过关键词、正则、substring、项目类型或语言来猜测普通文本是否为
continuation。无前台 Task 或前台 Task 已终止时创建新 Task 并注入 Session
上下文；可恢复的非终态 Task 按持久化状态恢复，并将用户原文作为 Steering。
审批、澄清和冲突仍使用各自的显式 Runtime 协议。

---

## 6. Session Context 简化

Context Assembler 只负责：

```python
context = {
    "recent_messages": ...,
    "task_summary": ...,
    "recent_artifacts": ...,
    "recent_tool_results": ...,
}
```

禁止恢复：

- runtime graph
- workflow graph
- execution DAG
- outcome dependency graph

目标：

让模型理解历史，
而不是恢复整个执行世界。

---

## 7. Outcome 重构

Outcome 从：

```text
Execution Gate
```

改成：

```text
Execution Record
```

新的职责：

- observability
- replay
- audit
- verification
- traceability

而不是：

```text
决定聊天能不能继续
```

### Tool Call 原则

observe 类工具：

- 自动归属 implicit observe outcome
- 不允许因 binding 缺失中断聊天

mutation / execute：

- 保持严格 runtime gating

---

## 8. Session Store 最小化

第一阶段只保证：

```text
CLI 重启后还能继续聊天
```

建议结构：

```text
sessions/
  <session_id>/
    messages.jsonl
    tasks.json
    summaries.json
```

避免提前实现：

- deterministic replay
- execution reconstruction
- workflow persistence

---

## 9. 实施阶段

### Phase 1 - 建立 Chat Core

目标：

- 普通聊天不经过 semantic router
- 多轮上下文稳定
- CLI 重启后可恢复
- 不因 outcome fail 中断

实施：

1. 建立 lightweight session pipeline
2. 引入 bounded context assembler
3. 收缩 active task 生命周期
4. 增加基于持久化 Task 状态的 continuation 选择
5. 为 observe tool 增加 fallback outcome

---

### Phase 2 - Runtime 解耦

目标：

- runtime 只负责副作用
- 普通 reasoning 不进入 deterministic flow

实施：

1. 拆分 conversation/runtime pipeline
2. runtime 只在 mutate/execute 前介入
3. steering 改成 side-channel
4. outcome 改成 post-execution record

---

### Phase 3 - 高级能力回接

在 chat core 稳定后，再逐步恢复：

- replay
- workflow
- verification
- orchestration
- advanced routing

所有高级能力必须满足：

```text
失效时不影响普通聊天
```

---

## 10. 验收标准

必须通过以下场景：

### 基础会话

- 连续聊天 30 分钟不丢上下文
- 普通追问不触发错误恢复
- 不会莫名 attach 到旧 task
- CLI 重启后能继续聊天

### Task 切换

- 新问题默认新 task
- continuation 能正确恢复
- completed task 不自动 resurrect
- interrupted task 可恢复

### Runtime 边界

- observe tool 不因 outcome fail 中断
- mutate/execute 仍需严格 runtime
- approval 不会被普通文本误触发

### 回归保护

- semantic router 挂掉时仍能聊天
- outcome binding 缺失时 observe 不失败
- replay/verification 异常不影响普通会话
