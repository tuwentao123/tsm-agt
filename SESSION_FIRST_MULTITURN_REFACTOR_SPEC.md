# Session-first 多轮对话主流程重构 SPEC

## 1. 问题

当前普通消息在进入主工程模型之前必须先经过独立 Session 路由模型。
路由模型的 ToolCall、JSON、置信度或 Task 关系任一校验失败，普通追问就
无法进入工程 Agent。结果是“继续分析”“基于上文整理文档”等基本多轮
交互被路由层截断。

## 2. 设计原则

- 普通自然语言直接进入主工程模型，不经过语义路由硬门禁。
- Harness 只按持久化 Session/Task 状态选择执行容器，不解释用户语义。
- 主工程模型通过有界 Session 上下文理解追问、新方向和产物引用。
- 审批、澄清、UNKNOWN_OUTCOME、Checkpoint、Trust、Sandbox、幂等继续
  由 Runtime 确定性控制。
- 不允许针对语言、项目类型或具体短语增加分支。

## 3. 普通输入状态映射

| 前台状态 | 行为 |
| --- | --- |
| 无前台 Task | 创建新 Task |
| 终态 Task | 创建新 Task，注入 Session 上下文 |
| INTERRUPTED / CONFLICT | 恢复同一 Task，将原始输入作为 Steering |
| AWAITING_USER + CONTINUATION | 恢复同一 Checkpoint，将原始输入交给主模型 |
| EXECUTING / RUNNING_WORKFLOW | 作为 Steering 在安全点合并 |
| AWAITING_APPROVAL | 保持审批协议；普通文本不构成批准 |
| AWAITING_USER + QUESTION | 保持澄清协议 |

显式 `/new`、`/resume`、`/use`、`/steer`、`/replace` 和 `/queue` 保持原协议。
只有显式 `/resume` 无 Task ID 且存在多个候选时才展示选择列表。

## 4. 上下文合同

新 Task 和恢复后的 Task 都接收：最近用户可见消息、全量轻量 Task 索引、
近期详细 Task 摘要、产物路径、当前 Checkpoint、工作区事实和最近工具结果。
上下文保持有界；历史原始工具正文留在事件账本中并按需读取。

## 5. 安全边界

- Session 上下文不继承审批、目录授权、进程所有权或未知副作用。
- 新 Task 不复用旧 Checkpoint。
- 恢复 Task 必须通过现有 Checkpoint 兼容性校验。
- 普通文本永远不解析为审批决定或澄清答案。

## 6. 验收

必须覆盖：独立问题；完成后追问；追问后整理文档；按文档实施；中断恢复；
Continuation 恢复；待审批不误批准；显式新建/恢复；长 Session 引用产物；
CLI 重启后继续；验证失败后同 Task 有界纠错并完成。
