# Agent 行动与结论事实可追溯设计 V2

## 1. 文档目标

本文是《Agent行动与结论事实可追溯设计.md》的 implementation-ready V2 版本。

V2 的目标不是推翻现有 runtime/event/projection/verification 架构，而是在当前 `tsm-agt` 已存在的：

- Event Log
- Runtime Event
- Tool Execution Ledger
- Verification Pipeline
- Flow Projection
- Mutation Journal
- Task/Turn State Machine
- Replay / Resume

基础之上，引入“可机器验证的 Conclusion Protocol”。

核心目标：

```text
模型可以动态行动。
Runtime 只记录可信事实。
结论必须引用事实。
事实必须可回放、可验证、可降级。
Task 完成状态必须与结论协议一致。
```

V2 重点解决 V1 中尚未工程化定义的问题：

- Conclusion Schema
- Structured Parser Protocol
- Event Reference Exposure
- Task/Turn 状态映射
- FactReference 生命周期
- Replay 语义
- Projection 降级规则
- Conclusion Ledger
- Transaction Boundary
- Runtime Authority Boundary
- Backward Compatibility
- Incremental Migration

---

# 2. V2 总体架构

```text
User Goal
    │
    ▼
Agent Runtime Loop
    │
    ├─ Tool Execution
    ├─ Runtime Event Recording
    ├─ Artifact Persistence
    ├─ Verification Observation
    └─ Conclusion Extraction
    │
    ▼
Conclusion Ledger
    │
    ├─ FactReference Validation
    ├─ Projection Downgrade
    ├─ Replay Compatibility
    └─ Task State Mapping
    │
    ▼
Projection / UI / Export
```

V2 不新增第二套 runtime。

V2 是：

```text
在现有 runtime 上增加“结论协议层”。
```

---

# 3. Conclusion Protocol

## 3.1 Conclusion 数据模型

```python
from dataclasses import dataclass
from enum import Enum
from typing import Literal


class ConclusionKind(str, Enum):
    IMPLEMENTED = "implemented"
    CHECKED = "checked"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    LIMITED = "verification_limited"
    HUMAN_CONFIRMATION_REQUIRED = "human_confirmation_required"


@dataclass
class FactReference:
    reference_type: Literal[
        "event",
        "tool_call",
        "artifact",
        "verification",
        "command_result",
    ]
    reference_id: str
    event_sequence: int | None = None
    replayable: bool = True
    retention_policy: str = "task_default"


@dataclass
class Conclusion:
    conclusion_id: str
    kind: ConclusionKind
    summary: str
    verified_scope: list[str]
    unverified_scope: list[str]
    fact_references: list[FactReference]
    warnings: list[str]
    created_at: str
    turn_id: str
    task_id: str
```

---

## 3.2 Structured Output Protocol

V2 不允许仅依赖自然语言猜测 Conclusion。

模型最终输出必须包含结构化 Conclusion Block。

推荐协议：

```markdown
<tsm_conclusion>
{
  "conclusions": [
    {
      "kind": "verified",
      "summary": "相关pytest已通过",
      "verified_scope": [
        "message renderer"
      ],
      "unverified_scope": [
        "真实浏览器交互"
      ],
      "fact_references": [
        {
          "reference_type": "tool_call",
          "reference_id": "call_abc123"
        }
      ]
    }
  ]
}
</tsm_conclusion>
```

Runtime 负责：

- parse
- validate
- persist
- downgrade

模型不直接写 TaskStatus。

---

## 3.3 Conclusion JSON Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ConclusionProtocol",
  "type": "object",
  "required": ["conclusions"],
  "properties": {
    "conclusions": {
      "type": "array",
      "items": {
        "type": "object",
        "required": [
          "kind",
          "summary",
          "fact_references"
        ],
        "properties": {
          "kind": {
            "enum": [
              "implemented",
              "checked",
              "verified",
              "verification_failed",
              "verification_limited",
              "human_confirmation_required"
            ]
          },
          "summary": {
            "type": "string"
          },
          "verified_scope": {
            "type": "array"
          },
          "unverified_scope": {
            "type": "array"
          },
          "fact_references": {
            "type": "array"
          }
        }
      }
    }
  }
}
```

---

# 4. Conclusion Parser Protocol

## 4.1 Parser 执行时机

Parser 必须在：

```text
final answer finalize 前
session.task_result_recorded 前
projection commit 前
```

执行。

禁止：

```text
仅在 UI 层解析 conclusion
```

因为这会导致 replay 与 projection 不一致。

---

## 4.2 Runtime Pipeline

```python
async def finalize_agent_turn(turn_context: TurnContext):
    model_output = turn_context.model_output

    parsed = conclusion_parser.parse(model_output)

    validated = conclusion_validator.validate(
        parsed,
        runtime_events=turn_context.runtime_events,
    )

    downgraded = projection_downgrade.apply(validated)

    ledger_id = await conclusion_ledger.persist(
        downgraded
    )

    await projector.project_conclusion(
        ledger_id
    )
```

---

## 4.3 Parser Failure Strategy

若 parser 失败：

```text
不阻止 turn 完成
但生成 parser_failure runtime event
并自动降级为 MODEL_CLAIM
```

Runtime 不允许因为 parser 失败直接宣布 Task FAILED。

---

# 5. FactReference 生命周期

## 5.1 生命周期状态

```text
ACTIVE
REPLAYABLE
ORPHANED
EXPIRED
PURGED
```

---

## 5.2 生命周期规则

| 状态 | 含义 |
|---|---|
| ACTIVE | 引用目标仍存在 |
| REPLAYABLE | replay/export 可重新解析 |
| ORPHANED | artifact/event 已丢失 |
| EXPIRED | retention policy 已过期 |
| PURGED | 被主动清理 |

---

## 5.3 Artifact Retention Policy

```python
class RetentionPolicy(str, Enum):
    TASK_DEFAULT = "task_default"
    SESSION = "session"
    PERSISTENT = "persistent"
    EXPORT_REQUIRED = "export_required"
```

以下事实默认必须 replayable：

- verification result
- command result
- screenshot metadata
- browser console snapshot
- structured test report

---

## 5.4 Orphan Handling

若 replay 时引用失效：

```text
不删除原 Conclusion
但标记：
FACT_REFERENCE_ORPHANED
```

Projection 必须显示：

```text
该结论原始证据已不可回放。
```

禁止静默隐藏。

---

# 6. Runtime Authority Boundary

V2 明确收缩 Runtime Authority。

Runtime 不负责：

- 业务修复语义判断
- 自动认定“问题解决”
- 自动生成恢复规划
- 推断用户满意度

Runtime 负责：

- 执行
- 审批
- 记录事实
- 保证事件顺序
- 验证引用有效性
- 管理 retention
- 提交 transaction

---

# 7. Task / Turn 状态映射

## 7.1 新增 ConclusionAwareTaskState

```python
class TaskResolution(str, Enum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    BLOCKED = "blocked"
    FAILED = "failed"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
```

---

## 7.2 映射规则

| Conclusion | TaskResolution |
|---|---|
| VERIFIED | SUCCESS |
| IMPLEMENTED + LIMITED | PARTIAL_SUCCESS |
| HUMAN_CONFIRMATION_REQUIRED | HUMAN_REVIEW_REQUIRED |
| VERIFICATION_FAILED | FAILED |
| parser failure | PARTIAL_SUCCESS |

---

## 7.3 Runtime Mapping Algorithm

```python
def derive_task_resolution(
    conclusions: list[Conclusion],
) -> TaskResolution:
    if any(c.kind == ConclusionKind.VERIFICATION_FAILED for c in conclusions):
        return TaskResolution.FAILED

    if any(
        c.kind == ConclusionKind.HUMAN_CONFIRMATION_REQUIRED
        for c in conclusions
    ):
        return TaskResolution.HUMAN_REVIEW_REQUIRED

    if any(c.kind == ConclusionKind.VERIFIED for c in conclusions):
        return TaskResolution.SUCCESS

    return TaskResolution.PARTIAL_SUCCESS
```

---

# 8. Projection Downgrade Rules

## 8.1 Downgrade Matrix

| 条件 | Projection |
|---|---|
| VERIFIED 但无 fact reference | MODEL_CLAIM |
| screenshot 缺失 | LIMITED |
| parser failure | UNSTRUCTURED_RESULT |
| replay orphan | HISTORICAL_UNVERIFIED |

---

## 8.2 降级原则

```text
宁可降级可信度，
也不允许伪造验证强度。
```

---

# 9. Event Reference Exposure

## 9.1 Runtime 必须向模型暴露

```text
execution_id
event_sequence
artifact_id
tool_call_id
verification_id
```

否则模型无法形成稳定引用。

---

## 9.2 Prompt 注入格式

```text
Tool Result:
- tool_call_id: call_xxx
- execution_id: exec_xxx
- event_sequence: 441
- artifacts:
  - artifact_screenshot_1
```

---

# 10. Replay 与 Resume 语义

## 10.1 Replay 要求

Replay 必须恢复：

- Conclusion Ledger
- FactReference
- Projection Downgrade
- TaskResolution

Replay 不要求恢复：

- 原始浏览器页面
- 外部服务实时状态
- 非持久 artifact 内容

---

## 10.2 Resume Compatibility

恢复任务时：

```text
历史 conclusion 不重新解释。
```

只允许：

```text
追加新的 conclusion。
```

避免历史语义漂移。

---

# 11. Conclusion Ledger

## 11.1 Ledger 结构

```python
@dataclass
class ConclusionLedgerRecord:
    ledger_id: str
    task_id: str
    turn_id: str
    conclusion_payload: dict
    validation_result: dict
    downgrade_result: dict
    replay_status: str
    created_at: str
```

---

## 11.2 Ledger 与 Runtime Event 的关系

```text
Runtime Event = 原始事实
Conclusion Ledger = 对事实的结构化结论
```

禁止：

```text
直接修改 Runtime Event 表达语义结论
```

---

# 12. Transaction Boundary

## 12.1 Atomic Commit Boundary

以下必须原子提交：

```text
final answer
runtime events
conclusion ledger
projection update
```

否则会出现：

```text
UI显示已验证
但ledger不存在
```

---

## 12.2 推荐事务顺序

```python
async with runtime.transaction():
    await event_store.persist(events)
    await ledger.persist(conclusions)
    await projector.persist(view_model)
    await final_answer_store.persist(answer)
```

commit 成功后才允许：

```text
Task turn finalized
```

---

# 13. Backward Compatibility

## 13.1 V1 兼容策略

V1 的自然语言 final answer：

```text
不强制失败。
```

Runtime 自动：

```text
infer weak conclusion
```

映射：

```text
UNSTRUCTURED_MODEL_CLAIM
```

---

## 13.2 渐进式迁移

### Phase 1

实现：

- Conclusion schema
- parser
- validator
- downgrade
- tests

不改 TaskState。

---

### Phase 2

实现：

- conclusion ledger
- replay integration
- projection integration
- artifact retention

---

### Phase 3

实现：

- TaskResolution mapping
- Runtime authority shrink
- completion readiness rewrite

---

### Phase 4

实现：

- UI traceability graph
- clickable fact reference
- replay explorer
- historical verification timeline

---

# 14. Validator 示例

```python
class ConclusionReferenceValidator:
    def validate(
        self,
        conclusion: Conclusion,
        runtime_events: list[RuntimeEvent],
    ) -> ValidationResult:
        existing_ids = {
            event.event_id
            for event in runtime_events
        }

        missing = []

        for ref in conclusion.fact_references:
            if ref.reference_id not in existing_ids:
                missing.append(ref.reference_id)

        if missing:
            return ValidationResult(
                valid=False,
                downgrade_to="MODEL_CLAIM",
                missing_references=missing,
            )

        return ValidationResult(valid=True)
```

---

# 15. Projection ViewModel 示例

```python
@dataclass
class ConclusionViewModel:
    title: str
    status: str
    confidence: str
    verified_scope: list[str]
    unverified_scope: list[str]
    evidence_links: list[str]
    downgrade_reason: str | None
```

---

# 16. 测试矩阵

| 场景 | 预期 |
|---|---|
| 模型宣称 VERIFIED 无引用 | downgrade |
| artifact 被删除 | orphan warning |
| parser failure | partial success |
| replay conclusion | status preserved |
| verification failed | task failed |
| human confirmation | review required |

---

# 17. 与当前 tsm-agt 架构兼容性

V2 默认兼容：

- runtime event store
- verification pipeline
- flow projector
- mutation journal
- task replay
- approval system
- runtime db
- session persistence

V2 不要求：

- 重写 runtime loop
- 推翻 task system
- 删除 verification gating
- 替换 projector

V2 是：

```text
在现有 runtime 上增加可信结论层。
```

---

# 18. 最终原则

## 18.1 Runtime 不制造语义

Runtime 只能记录：

```text
发生了什么
```

不能决定：

```text
用户是否满意
业务是否真正正确
```

---

## 18.2 模型不能伪造验证

没有事实引用：

```text
不能声称 verified
```

---

## 18.3 Projection 不得增强可信度

Projection 只能：

```text
保留或降低可信度
```

不能提升。

---

## 18.4 Replay 必须保持历史真实性

恢复历史时：

```text
允许缺失事实。
不允许改写历史结论。
```
