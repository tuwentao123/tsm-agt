# Runtime执行环境错误恢复与重新验证SPEC

状态：待实施

关联问题：`实测记录与优化问题本.md` P044

关联现象：

- `core.run_command` 在验证阶段拒绝相对 executable；
- Runtime 未向 Agent 暴露结构化恢复语义；
- 验证阶段一度被误判为逻辑失败或阻塞；
- 需要重新建立 Runtime Error Recovery 与自动恢复能力。

---

## 1. 问题背景

在多轮会话 Planner 意图识别修复的重新验证过程中，Agent 执行：

```text
.venv/bin/python -m pytest ...
```

Runtime 在真正启动 subprocess 之前直接拒绝命令。

由于当前 Runtime 只返回通用失败，而没有提供结构化错误语义，Agent 无法明确判断：

- 是 pytest 测试失败；
- 还是 Runtime policy 拒绝；
- 还是 executable 路径不合法；
- 是否应该自动重试；
- 应该如何修复参数。

最终导致：

- 验证阶段被错误记录为阻塞；
- 已实现代码一度缺少有效验证结论；
- Agent 的恢复能力依赖人工分析。

后续改为绝对路径 executable 后：

```text
/Users/tusima/Documents/学习/tsm-agt/.venv/bin/python -m pytest ...
```

测试恢复正常。

说明问题本质属于：

```text
Runtime Execution Recoverability Gap
```

而不是业务代码逻辑失败。

---

## 2. 当前问题分析

### 2.1 当前执行链

当前命令执行链：

```text
LLM
  ↓
ToolCall
  ↓
Kernel
  ↓
Runtime / ToolRuntime
  ↓
subprocess supervisor
  ↓
OS / Sandbox / Policy
```

当前 Runtime 的主要问题不是“执行失败”，而是：

```text
错误语义在 Runtime 层丢失
```

Agent 最终只能得到：

```text
tool failed
```

而不是：

```json
{
  "error_type": "relative_executable_not_allowed",
  "retryable": true,
  "agent_fixable": true,
  "suggested_fix": "convert_to_absolute_path"
}
```

---

### 2.2 当前缺失能力

当前 Runtime 缺少：

- 结构化 ToolError；
- Runtime Error 分类；
- Agent 可恢复错误模型；
- 自动参数修复提示；
- Retry Policy；
- ToolSpec 级别 executable/path 约束；
- 统一恢复诊断信息。

因此模型无法形成：

```text
发现错误
→ 理解错误类型
→ 自动修复参数
→ 自动重试
→ 恢复验证
```

这一恢复链。

---

## 3. 修复目标

本次修复目标：

1. Runtime 对命令执行错误提供结构化语义；
2. Agent 能识别“可恢复错误”；
3. Agent 能根据错误自动修复参数并重试；
4. 验证阶段不再把 Runtime policy 错误误判为逻辑失败；
5. ToolRuntime 能明确表达 executable/path 约束；
6. Runtime 日志与 Ledger 可审计恢复行为；
7. 建立稳定的重新验证流程。

本次不包含：

- 全自动 shell 自愈系统；
- 任意命令自动修复；
- 放宽 Sandbox 安全策略；
- 绕过审批或 Capability 限制。

---

## 4. 修复方案

### 4.1 引入统一 ToolError 模型

新增统一 Runtime 错误结构：

```python
class ToolError:
    category: str
    code: str
    retryable: bool
    agent_fixable: bool
    user_fixable: bool
    suggested_actions: list[str]
    diagnostic_message: str
```

典型分类：

| category | 含义 |
|---|---|
| runtime_policy | Runtime/Sandbox 拒绝 |
| invalid_argument | 参数不合法 |
| timeout | 执行超时 |
| permission | 权限问题 |
| environment | 环境缺失 |
| transient_io | 可恢复 IO 问题 |
| process_failure | 命令自身失败 |

---

### 4.2 Runtime Error Mapping

对 subprocess / sandbox 异常建立统一映射。

示例：

| 原始异常 | 映射错误 |
|---|---|
| FileNotFoundError | executable_not_found |
| PermissionError | permission_denied |
| TimeoutExpired | command_timeout |
| relative executable rejection | relative_executable_not_allowed |
| invalid cwd | invalid_working_directory |

Runtime 必须返回：

- 结构化 code；
- 是否可重试；
- 推荐修复动作；
- 可读诊断。

---

### 4.3 Kernel 自动恢复策略

Kernel 不再只是透传错误。

新增：

```text
Agent-fixable Runtime Recovery
```

恢复逻辑示例：

```text
relative executable
→ 转换绝对路径
→ retry once
```

```text
cwd missing
→ fallback workspace root
→ retry once
```

```text
shell quoting issue
→ escape parameters
→ retry once
```

恢复次数必须有界，避免无限循环。

---

### 4.4 ToolSpec 执行约束声明

在 ToolSpec 中增加：

```python
requires_absolute_executable=True
```

Planner / Agent 在生成命令时可提前规避非法参数。

目标：

```text
错误前移
```

而不是：

```text
失败后猜测修复
```

---

### 4.5 Ledger 与审计

恢复行为需要记录：

- 原始错误；
- Runtime mapping；
- 自动修复动作；
- retry 次数；
- 最终执行结果。

避免：

```text
Agent silently retries
```

导致行为不可审计。

---

## 5. 重新验证计划

修复后需重新验证以下场景。

### 5.1 相对 executable 自动恢复

输入：

```text
.venv/bin/python -m pytest
```

预期：

- Runtime 返回结构化错误；
- Kernel 自动转换绝对路径；
- 自动重试成功；
- Ledger 记录恢复过程。

---

### 5.2 不可恢复 executable

输入不存在的 Python 路径。

预期：

- 返回 executable_not_found；
- 不无限重试；
- Agent 明确要求人工修复。

---

### 5.3 timeout 分类

构造超时命令。

预期：

- 返回 command_timeout；
- 正确区分 Runtime timeout 与 command failure；
- 保留 stdout/stderr。

---

### 5.4 验证阶段恢复

重新执行：

```text
pytest tests/test_core_readonly_tools.py
```

预期：

- 验证流程正常完成；
- 不再误判 BLOCKED；
- Completion/Acceptance 正确获得验证证据。

---

## 6. 完成标准

满足以下条件后，视为本 SPEC 完成：

1. Runtime 提供统一结构化 ToolError；
2. relative executable 错误可自动恢复；
3. Kernel 支持有界自动 retry；
4. Ledger 可记录恢复行为；
5. 验证阶段不再错误进入 BLOCKED；
6. 回归测试覆盖 Runtime Error Mapping；
7. 多轮会话 Planner 修复重新验证通过；
8. 不引入绕过 Sandbox/审批的新风险。
