# 多轮会话 Planner 意图识别优化 SPEC

状态：草案，待实施

关联问题：`实测记录与优化问题本.md` P051

关联任务：

- 错误实施 Task：`task-b214fc7362ca436ba4ac7c7a1d529312`
- 被引用的失败 source Task：`task-c91e87187c63411f95026db60d638b14`

---

## 1. 目标

在保留现有 Session Resolver、TaskSpec Planner、Agent Loop 和验证流程的前提下，完善 Planner 的输入接线，使 Planner 在多轮 FOLLOW_UP 场景中同时获得：

1. 用户本轮原始请求；
2. 最新且有界的 Session 上下文；
3. Resolver 已选择的 source Task 结构化历史；
4. 当前 Runtime 能力事实。

解决历史 Task 的旧 `remaining_work` 被混入派生 Goal 后，因 Planner 看不到后续 Session 决策而被错误提升为当前 TaskSpec 的问题。

本次优化不承诺模型永远不会发生语义判断错误；目标是确保模型获得真实、最新、来源清晰的判断材料，并避免 Runtime 在 Planner 之前丢失或混淆这些材料。

---

## 2. 设计原则

```text
模型负责语义判断
Runtime 负责事实、来源、版本和状态校验
Planner 负责把当前意图转换成验收合同
Agent 负责动态执行
Completion/Acceptance 负责根据事实验证结果
```

具体原则：

1. 不推翻现有主流程，只优化 Handoff 到 Planner 的输入接线。
2. 当前用户原话必须作为 Planner 的直接 `goal`，不能只存在于派生 Goal 内部。
3. 历史 Task 的 `remaining_work` 保留，但明确标记为历史模型计划，不自动等同于当前验收要求。
4. 较新的 Session 用户决策和消息必须对 Planner 可见。
5. Runtime 不判断 capability、环境继承或具体业务方案哪个正确。
6. Planner 不新增自己的上下文限界，复用 `SessionContextProjector`。
7. 第一批不改变 TaskSpec 输出 schema、不改变 Agent Loop、不删除旧 Handoff。
8. 规划输入必须可审计、可重建，但事件中不重复保存敏感原文。

---

## 3. 当前流程和职责

```text
用户输入
    ↓
ModelSessionInputResolver
模型判断 relation 和 source_task_id
    ↓
Runtime
校验 Task ID、状态、关系组合
    ↓
build_session_follow_up_goal
拼接当前请求和 source Task 摘要
    ↓
TaskSpec Planner
生成 scope / constraints / acceptance criteria / outcomes
    ↓
Runtime
校验、持久化并以 SYSTEM role 注入 TaskSpec
    ↓
Agent Loop
动态读取、修改和验证
    ↓
Runtime Ledger
记录 Tool / Process / Mutation / Evidence 事实
    ↓
Completion Readiness / Final Acceptance
判断是否结束
```

各层正确职责：

| 层级 | 负责 | 不负责 |
|---|---|---|
| Session Resolver | 判断当前输入与历史 Task 的语义关系 | 编写新 Goal、授权、执行工具 |
| Runtime | 校验 ID、状态、权限、版本和来源真实性 | 判断历史方案是否仍正确 |
| Session/Handoff | 提供当前请求和历史上下文 | 把历史模型计划直接确定为当前目标 |
| TaskSpec Planner | 根据完整材料判断当前意图并生成验收合同 | 读取文件、执行工具、分配 Runtime 状态 |
| Agent | 动态决定读取、修改、命令和恢复方式 | 伪造 Runtime 事实 |
| Runtime Ledger | 记录执行事实 | 判断业务目标语义 |
| Completion/Acceptance | 核验证据和合同完成状态 | 修正 Planner 没看到的历史语义 |

---

## 4. 当前问题

### 4.1 Planner 输入过少

当前 `Kernel.plan_task_spec()` 实际调用：

```python
raw = await planner.propose_task_spec(current.goal, {
    "workspace": task.workspace,
    "available_tool_effects": sorted(...),
})
```

Planner 最终收到：

```json
{
  "goal": "<派生 handoff Goal>",
  "runtime_context": {
    "workspace": "...",
    "available_tool_effects": ["observe", "mutate", "execute"]
  }
}
```

### 4.2 派生 Goal 混合了不同来源

`build_session_follow_up_goal()` 将以下内容拼在同一字符串中：

```text
Current request
source Task ID / state / verification
source Task original goal
source Task remaining_work
source Task completed_work
historical outcomes
```

其中 Task ID、状态和 verification 是 Runtime 事实；`remaining_work` 和旧 Goal 包含历史模型的语义判断。Planner 只能从混合字符串中自行区分来源。

### 4.3 Planner 看不到 source Task 之后的 Session 变化

当前 Planner 不接收：

- 用户本轮原始输入的独立字段；
- 最新 Session messages；
- Session working state 中的 decisions/constraints/open questions；
- 最近创建或引用的 artifacts；
- source Task 的结构化状态和历史计划属性。

因此在 P051 中，Planner 看到了失败 Task 的旧 capability 计划，但没有看到后续已经改成进程环境继承方案的决策。

### 4.4 错误会被 TaskSpec 权威放大

Planner 输出经 schema 校验后直接持久化为 TaskSpec，并以 SYSTEM role 注入主 Agent。后续 Agent 会优先服从错误 TaskSpec，导致错误语义转化为真实 mutation。

---

## 5. 目标 Planner 输入结构

`TaskSpecPlannerPort` 保持不变：

```python
async def propose_task_spec(
    self,
    goal: str,
    context: Mapping[str, Any],
) -> Mapping[str, Any]: ...
```

第一版只调整参数内容。

### 5.1 `goal`

当前：

```text
TaskSpecSnapshot.current.goal（派生 handoff 字符串）
```

目标：

```text
用户本轮原始输入 session.task_attached.payload.user_text
```

示例：

```text
那开始实施阶段3代码吧
```

### 5.2 `runtime_context`

目标结构：

```json
{
  "workspace": "/path/to/workspace",
  "available_tool_effects": [
    "observe",
    "mutate",
    "execute"
  ],
  "session": {
    "revision": 327,
    "working_state": {
      "constraints": [],
      "decisions": [
        "废弃 capability 方案",
        "采用进程环境继承方案",
        "默认采用 core"
      ],
      "open_questions": []
    },
    "recent_messages": [],
    "recent_artifacts": []
  },
  "related_task": {
    "task_id": "task-c91...",
    "state": "FAILED",
    "verification_status": "blocked",
    "goal": "<历史 Goal>",
    "historical_remaining_work": [
      "实现 capability 模型",
      "修改 Kernel"
    ],
    "completed_work": [],
    "mutations": []
  }
}
```

### 5.3 字段语义

| 字段 | 语义 |
|---|---|
| `goal` | 当前用户直接请求，Planner 的首要目标来源 |
| `workspace` | Runtime 已规范化的当前工作区 |
| `available_tool_effects` | 当前 Runtime 可提供的工具效果类型 |
| `session.revision` | 本次规划使用的 Session 版本 |
| `session.working_state` | 最新 Session constraints/decisions/open questions |
| `session.recent_messages` | 解释“这个、继续、刚才、阶段3”等指代的最近消息 |
| `session.recent_artifacts` | 当前 Session 最近记录的候选资源路径，不代表内容已读取 |
| `related_task` | Resolver 已选择的 source Task 结构化历史 |
| `historical_remaining_work` | source Task 的旧模型计划，只供判断，不自动成为验收合同 |

---

## 6. Planner 判断规则

`ModelTaskSpecPlanner` 的 system instruction 增加：

```text
1. goal 是当前用户的直接请求。
2. runtime_context.session 用于解释 goal 中依赖会话历史的指代。
3. related_task 是历史上下文；historical_remaining_work 来自历史模型计划，不是 Runtime 事实。
4. 较新的用户决策优先于较旧 Task 的计划。
5. 不得在未结合最新 Session 上下文的情况下，将 historical_remaining_work 直接复制为 scope、constraints 或 acceptance criteria。
6. recent_artifacts 只证明资源路径被记录过，不证明 Planner 已读取其内容；不得根据文件名虚构具体要求。
7. 当材料不足以确定详细实现范围时，生成面向结果的粗粒度验收合同，不得自行补造具体架构方案。
```

这些规则属于模型语义判断，不在 Kernel 中增加业务关键词分支。

---

## 7. Session 上下文复用和限界

Planner 不新增第二套最近消息、Task、artifact 数量配置，复用现有 `SessionContextProjector`：

```text
recent_visible_message_limit = 8
detailed_task_summary_limit = 4
historical_resource_limit = 24
historical_question_limit = 12
recent_execution_limit = 10
recent_execution_per_task_limit = 4
```

要求：

1. `SessionContextProjector` 继续负责统一限界；
2. `SessionPromptProjection` 同时提供结构化 `prompt_data` 和模型消息 `message`；
3. 主 Agent 使用 `message`；
4. Planner 使用 `prompt_data` 中需要的字段；
5. 不允许 Planner 再次自行截断同一份数据。

引用完整性：

- Resolver 已选择的 `source_task_id` 对应 summary 必须固定保留，不受 `detailed_task_summary_limit` 限制；
- source Task 直接关联的 artifact 优先保留；
- 其他 Task、artifact、执行记录继续使用现有限界；
- 固定保留只保证引用数据完整，不代表 Runtime 认同历史方案。

---

## 8. Runtime 验证和可观测性

Runtime 不验证 capability 与环境继承哪个方案正确，只验证 Planner 使用的材料是否真实、新鲜。

`task_spec.revised` 增加：

```json
{
  "planning_session_revision": 327,
  "planning_context_hash": "...",
  "current_request_hash": "..."
}
```

字段用途：

- `planning_session_revision`：记录 Planner 使用的 Session 版本；
- `planning_context_hash`：证明本次 TaskSpec 对应哪份结构化输入；
- `current_request_hash`：证明当前用户请求未被其他派生文本替换。

第一版仅记录，不因 Session revision 变化阻断或自动重试。观察到真实并发陈旧问题后，再增加“提交 TaskSpec 时 Session revision 不一致则重新规划一次”的乐观并发校验。

不得在事件中重复保存：

- 完整用户原文；
- 完整 Session 消息正文；
- 敏感工具参数；
- 文件正文。

---

## 9. 具体改造点

### 9.1 构造 Planner Planning Context

在 Kernel 中新增内部辅助方法，名称可在实施时按现有风格确定：

```python
async def _task_spec_planning_context(
    self,
    task: TaskSnapshot,
) -> tuple[str, Mapping[str, Any], int, str]:
    """返回 current_request、bounded context、session revision 和 context hash。"""
```

职责：

1. 从 Session 投影取得当前 Task 的用户原始输入；
2. 读取统一的结构化 Session prompt data；
3. 根据 `source_task_id` 取得 related Task summary；
4. 将 source `remaining_work` 映射为 `historical_remaining_work`；
5. 组装 workspace、available effects、session、related task；
6. 计算 canonical context hash。

该方法只能组装和校验数据，不判断历史方案语义。

### 9.2 调整 `Kernel.plan_task_spec()`

当前：

```python
raw = await planner.propose_task_spec(current.goal, {
    "workspace": task.workspace,
    "available_tool_effects": ...,
})
```

目标：

```python
current_request, planning_context, session_revision, context_hash = (
    await self._task_spec_planning_context(task)
)

raw = await planner.propose_task_spec(
    current_request,
    planning_context,
)
```

### 9.3 调整 `ModelTaskSpecPlanner`

保持：

- 一次 Planner 模型调用；
- `planner.submit_task_spec` 工具 schema；
- `TaskSpecProposal` 输出；
- 一次有界协议纠正。

只修改 system instruction 和输入字段说明。

### 9.4 记录规划来源

在 `task_spec.revised` payload 中增加第 8 节三个审计字段。

### 9.5 Handoff 暂时保持兼容

第一批不删除或改写：

```text
build_session_follow_up_goal()
TaskSnapshot.goal
SDK/CLI/Web 的派生 Task 创建流程
```

Planner 不再把派生 Goal 当唯一语义输入。完成真实复测后，再决定 derived Goal 是否降级或移除。

---

## 10. 实施阶段

### 阶段一：结构化输入接线

- `SessionPromptProjection` 暴露可复用的结构化 `prompt_data`；
- source Task 固定保留；
- Kernel 构造 Planner planning context；
- Planner 使用当前用户原话作为 `goal`；
- `historical_remaining_work` 明确标注历史属性；
- 更新 Planner system instruction。

阶段一不改变 Agent Loop、TaskSpec 输出和结束判定。

### 阶段二：规划来源审计

- 写入 `planning_session_revision`；
- 写入 `planning_context_hash`；
- 写入 `current_request_hash`；
- 增加事件投影和故障诊断展示。

### 阶段三：基于实测决定是否收缩 derived Goal

经过 P051 同形任务和普通多轮任务复测后再评估：

- derived Goal 是否仅保留用于 UI；
- 是否改为 current request + source_task_id；
- 是否删除 `build_session_follow_up_goal()`。

阶段三不是本次首批必需改动。

---

## 11. 非目标

本 SPEC 不做：

- 不删除 `ModelSessionInputResolver`；
- 不删除 FOLLOW_UP/BRANCH/CONTINUE；
- 不新增 Grounding 状态机；
- 不新增普通 Task/Spec Task 两套核心流程；
- 不修改 TaskSpec 输出 schema；
- 不让 Planner 读取文件或执行工具；
- 不修改 Agent Loop；
- 不修改 Approval、Project Trust、Sandbox、取消或 Checkpoint；
- 不根据“阶段3”“SPEC”“capability”等业务关键词写 Runtime 分支；
- 不增加上下文条数来掩盖输入结构问题；
- 不保证模型永远不会发生语义判断错误。

---

## 12. 测试和验收

### 12.1 Planning Context 单元测试

- 当前 Task 的 `session.task_attached.user_text` 独立成为 Planner `goal`；
- 派生 Goal 不再作为 Planner 唯一目标；
- source Task 状态、verification、completed work、mutation 正确投影；
- source `remaining_work` 映射为 `historical_remaining_work`；
- Planner context 包含最新 Session revision 和 working state；
- source Task 超出最近 Task 限界时仍固定保留；
- source artifact 优先保留；
- 其他上下文仍满足 `SessionContextProjector` 限界；
- canonical context hash 对相同输入稳定。

### 12.2 Planner Adapter 测试

使用固定模型桩验证：

- Planner prompt 中当前请求和历史计划来源分离；
- 较新的 Session decisions 可见；
- historical remaining work 不会作为唯一输入；
- Planner 输出 schema 和既有协议纠正保持兼容。

### 12.3 P051 故障重放

构造：

```text
旧 Task：FAILED/blocked，历史 remaining_work 指向方案 A
后续 Session：用户明确废弃方案 A，采用方案 B
后续 artifact：生成方案 B 的实施文档
当前输入：开始实施该阶段
```

验收：

1. Planner 输入同时包含旧计划和后续决策；
2. Planner `goal` 等于当前用户原话；
3. 方案 A 不再是唯一具体实施方向；
4. 固定模型桩生成方案 B 对应 TaskSpec；
5. 主 Agent 首次探索读取方案 B artifact；
6. 不直接修改方案 A 涉及的文件。

第 5、6 项验证完整运行流程，不能只断言 Planner JSON。

### 12.4 兼容回归

- 自包含新任务仍正常规划；
- 普通 FOLLOW_UP 仍可使用 source Task 历史；
- FAILED Task 历史计划仍然可见；
- Session Context 大小不突破现有限界；
- SDK、CLI、Web 构造一致的 Planner 输入；
- TaskSpec revision、Completion Readiness、Final Acceptance 行为不变；
- 全量测试、编译检查和 `git diff --check` 通过。

---

## 13. 完成标准

满足以下条件才视为本 SPEC 完成：

1. Planner 不再只接收派生 handoff Goal；
2. 当前用户原话作为直接 `goal`；
3. Planner 获得最新 bounded Session context；
4. selected source Task 和关联 artifact 不被限界裁掉；
5. historical remaining work 有明确来源标识；
6. TaskSpec revision 可追溯到 planning session revision/context hash；
7. P051 同形故障重放通过；
8. 现有 Session、TaskSpec、Agent Loop 和验证回归通过。
