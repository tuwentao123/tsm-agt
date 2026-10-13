# Conversation Loop 证据机制移除与 Turn Finalization 重构 Spec

状态：Proposed，达到可实施标准
目标版本：下一次 Agent Runtime 架构版本
实施范围：`CONVERSATION_LOOP` 主路径
不实施范围：在迁移前继续扩展 `SINGLE_LOOP`、`LEGACY` 及旧 Completion Gap 语义

## 1. 决策摘要

本 Spec 作出以下不可逆架构决策：

1. 普通用户输入只进入 `CONVERSATION_LOOP`；不再通过 `ANSWER`、`SINGLE_LOOP` 或 `LEGACY` 形成无工具旁路。
2. 删除通用 `EvidenceQuestion` 机制。模型调用工具时只生成工具名和工具参数，不生成 `question_id`、`expected_scope` 或 `scope_expansion_reason`。
3. 删除现有 `CompletionReadinessPolicyPort`、`CompletionGap`、`CompletionReadinessState` 及其三种 mode。
4. 普通 assistant 文本不立即等于 Turn 完成，而是一个 `FinalizationCandidate`。
5. 候选回答依次通过：
   - Runtime Finalization Gate：确定性协议与运行状态校验；
   - Stop Policy Hooks：可选、确定性的领域门禁；
   - Completion Reviewer：仅在显式 Goal/长时自主任务中按需开启的模型语义审查。
6. Harness 不根据用户文本识别“源码分析”“问答”“编码”等意图，不增加关键词或规则分类器。
7. 普通 Turn 由主 Agent 根据 Prompt 和工具结果判断是否满足请求；Completion Reviewer 仅是显式开启的可选第二层，它不能调用工具，只能返回 `ACCEPT` 或 `CONTINUE`。
8. Reviewer 连续拒绝达到上限时，Turn 以 `INCOMPLETE` 结束，不得伪装成成功。
9. 旧 Checkpoint/Event 必须保持可读；历史字段只参与兼容读取，不进入新运行状态，也不再写入新 Checkpoint。

## 2. 目标与非目标

### 2.1 目标

- 降低模型工具调用协议复杂度，提高正确工具选择和参数生成成功率。
- 消除 Harness 中与模型竞争的第二套任务理解状态机。
- 阻止“需要源码/工作区/外部事实，但零工具直接收口”成为无条件成功路径。
- 保留审批、取消、恢复、持久化、工具批次、预算与多轮会话能力。
- 将完成判断设计成独立、可测试、可关闭、可观测的 Turn 终止阶段。
- 与 Codex/Hermes 的主循环形态对齐：模型决定工具，Harness 管理执行边界和停止边界。

### 2.2 非目标

- 本 Spec 不设计 OS 沙箱。
- 本 Spec 不设计 Tool Search/渐进工具发现；它可在本迁移完成后独立实施。
- 本 Spec 不创建 Harness 侧 `turn_intent`、任务类型分类器或源码关键词表。
- 本 Spec 不要求每个 Turn 至少调用一个工具。
- 本 Spec 不实现逐句 Claim→Artifact 强引用系统。
- 本 Spec 不改变 Tool Risk、Approval、Sandbox、Mutation Journal 的权限语义。
- 本 Spec 不迁移旧 `SINGLE_LOOP`/`LEGACY` 的完成合同；这些路径应删除而不是继续维护。

## 3. 目标运行流程

```text
User Message
    ↓
CONVERSATION_LOOP
    ↓
Model Response
    ├── contains tool calls
    │      ↓
    │   validate → approve/suspend → execute → persist result
    │      ↓
    │   append tool results → next model call
    │
    └── contains no tool calls
           ↓
       FinalizationCandidate
           ↓
       Runtime Finalization Gate
           ↓
       Stop Policy Hooks
           ↓
       Completion Reviewer（配置开启时）
           ├── ACCEPT   → commit final assistant response
           └── CONTINUE → append ephemeral correction → next model call
```

任何一层都不得自行选择业务工具。只有主 Agent 模型可以产生业务 ToolCall。

## 4. 核心不变量

实现和测试必须满足以下不变量：

### INV-F1：模型拥有意图和工具选择权

Runtime 不读取用户文本来决定任务类型，不根据“源码”“对比”“查询”等词强制调用工具。

### INV-F2：普通文本只是候选完成

`parsed_calls == ()` 只表示模型没有继续调用工具，不直接表示 Task 已完成。

### INV-F3：Runtime Gate 不做语义判断

Runtime Gate 只能依据持久化或内存中的确定性事实：待执行调用、审批、取消、FinishReason、ToolBatch 状态、预算和持久化结果。

### INV-F4：普通 Turn 的语义完成判断归主 Agent

主 Agent 通过稳定 Prompt、few-shot 和同一上下文中的工具结果，判断是否还需要外部事实、源码、工作区检查或更多步骤。Runtime 不另建意图分类器。Completion Reviewer 只在显式开启的 Goal/长时工作流中提供第二层审查。

### INV-F5：Reviewer 不得执行工具

Reviewer 请求必须满足：

```python
tools=()
allow_tool_calls=False
purpose=ModelCallPurpose.COMPLETION_REVIEW
```

### INV-F6：Continuation 有界

一个 Turn 最多允许 `max_review_continuations` 次 Reviewer 驱动的继续。默认值为 2。

### INV-F7：耗尽不是成功

超过 Reviewer continuation 上限、Reviewer 在强制模式下不可用、或 Runtime Gate 发现不可恢复异常时，必须返回 `AgentTurnIncomplete` 或失败状态，不得提交 `turn.completed`。

### INV-F8：内部纠正消息不进入会话历史

Reviewer continuation 使用 ephemeral scaffolding。它可进入当前模型请求和 Checkpoint，但不得成为用户下一轮看到的持久化 User Message。

### INV-F9：历史数据可读

含 `evidence_question`、`evidence_question_state`、`completion_readiness_state` 的历史 ToolCall 和 Checkpoint 必须能够恢复或安全转成新的运行结构。

## 5. 新增端口和数据类型

新增文件：

```text
src/tsm_agt/ports/turn_finalization.py
```

### 5.1 FinalizationAction

```python
class FinalizationAction(StrEnum):
    ACCEPT = "ACCEPT"
    CONTINUE = "CONTINUE"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"
```

```python
@dataclass(frozen=True, slots=True)
class FinalizationDecision:
    action: FinalizationAction
    reason_code: str
    instruction: str = ""

    def __post_init__(self) -> None:
        if not self.reason_code.strip() or len(self.reason_code) > 64:
            raise ValueError("finalization reason_code must contain 1..64 characters")
        if len(self.instruction) > 2_000:
            raise ValueError("finalization instruction must not exceed 2,000 characters")
```

含义：

- `ACCEPT`：允许提交候选 assistant message。
- `CONTINUE`：向主 Agent 注入一次纠正消息并继续同一 Turn。
- `INCOMPLETE`：到达可恢复但未完成的终止边界。
- `FAILED`：运行时不变量损坏或不可恢复错误。

### 5.2 ToolExecutionSummary

```python
@dataclass(frozen=True, slots=True)
class ToolExecutionSummary:
    call_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    ok: bool
    error_code: str = ""
    truncated: bool = False
    result_excerpt: str = ""
    result_meta: Mapping[str, Any] = field(default_factory=dict)
```

约束：

- `result_excerpt` 每项最多 2,000 字符；总 Reviewer 输入中的工具结果摘录最多 12,000 字符。
- 必须经过现有 secret redaction；不得把凭据、完整环境变量或未脱敏 header 交给 Reviewer。
- `arguments` 必须使用现有 ToolArgumentPresenter 或同等脱敏器。
- 只包含当前 Turn 的工具执行。

### 5.3 FinalizationCandidate

```python
@dataclass(frozen=True, slots=True)
class FinalizationCandidate:
    task_id: str
    turn_id: str
    source_user_message: Message
    assistant_message: Message
    finish_reason: FinishReason
    tool_executions: tuple[ToolExecutionSummary, ...]
    model_calls: int
    tool_calls: int
    review_continuations: int
```

### 5.4 CompletionReviewVerdict

```python
class CompletionReviewAction(StrEnum):
    ACCEPT = "ACCEPT"
    CONTINUE = "CONTINUE"


@dataclass(frozen=True, slots=True)
class CompletionReviewVerdict:
    action: CompletionReviewAction
    reason_code: str
    reason: str
    missing: tuple[str, ...] = ()
```

字段约束：

- `reason_code` 为稳定机器码，长度 1..64。
- `reason` 长度不超过 1,000 字符。
- `missing` 最多 8 项，每项不超过 300 字符。
- Reviewer 不返回工具名、工具参数或执行计划；下一步仍由主 Agent 决定。

### 5.5 CompletionReviewerPort

```python
class CompletionReviewerPort(RuntimeAdapter, Protocol):
    async def review(
        self,
        candidate: FinalizationCandidate,
    ) -> CompletionReviewVerdict: ...
```

首个实现：

```text
src/tsm_agt/adapters/model_completion_reviewer/reviewer.py
```

该 Adapter 复用 `ModelProviderPort`，但必须使用独立 `ModelRequest`，不得递归进入 Agent Loop。

## 6. Runtime Finalization Gate

新增文件：

```text
src/tsm_agt/core/conversation_loop/finalization.py
```

新增类：

```python
class RuntimeFinalizationGate:
    async def evaluate(
        self,
        state: TurnState,
        response: ModelResponse,
    ) -> FinalizationDecision: ...
```

Runtime Gate 不作为 Adapter，不允许被替换成业务语义策略。

### 6.1 判定顺序

必须严格按以下顺序执行：

1. `FinishReason.CANCELLED` → `INCOMPLETE(reason="model_cancelled")`
2. `FinishReason.ERROR` → `FAILED(reason="model_error")`
3. `FinishReason.LENGTH`：
   - 尚有 model budget → `CONTINUE(reason="output_truncated")`
   - budget 耗尽 → `INCOMPLETE(reason="output_truncated_budget_exhausted")`
4. `state.pending_tool_calls` 非空 → `FAILED(reason="pending_tool_calls_at_finalization")`
5. `checkpoint.tool_batch` 存在且仍有 pending call ID → `FAILED(reason="tool_batch_not_terminal")`
6. `checkpoint.pending_user_action` 非空 → 返回现有 suspension，而不是完成。
7. 最后一次 ToolCall 缺少匹配 ToolResult → `FAILED(reason="unmatched_tool_call")`
8. 当前候选回答为空：
   - 有 budget → `CONTINUE(reason="empty_final_response")`
   - 无 budget → `INCOMPLETE(reason="empty_response_budget_exhausted")`
9. 其他情况 → `ACCEPT`，进入 Stop Hook 和 Reviewer；此处的 ACCEPT 仅表示 Runtime 状态允许收口，不代表语义完成。

### 6.2 Runtime continuation 消息

`output_truncated` 和 `empty_final_response` 使用固定、非语义消息：

```json
{
  "type": "runtime_finalization_continuation",
  "reason": "output_truncated",
  "instruction": "Continue the same turn. Complete the response or issue the tool call still required."
}
```

新增 scaffolding kind：

```text
runtime-finalization
```

## 7. Stop Policy Hook

新增端口：

```python
class TurnStopPolicyPort(RuntimeAdapter, Protocol):
    async def evaluate(
        self,
        candidate: FinalizationCandidate,
    ) -> CompletionReviewVerdict | None: ...
```

用途仅限确定性领域规则，例如：

- 修改了代码但配置要求执行测试；
- 启动了声明为 attached 的后台进程但未等待；
- mutation journal 尚未进入终态；
- 部署/发布工具返回明确的异步 pending 状态。

禁止实现：

- 根据用户文本识别任务类型；
- 根据“源码”“搜索”等关键词强制工具；
- 推断回答事实是否正确；
- 在 Hook 内直接调用业务工具。

首版可以不注册任何 Stop Policy。接口先用于替代旧 Completion Gate 的扩展位置。

多个 Hook 的执行规则：

1. 按 Adapter 注册顺序执行；
2. 任一返回 `CONTINUE` 即停止后续 Hook，进入 continuation；
3. 返回 `None` 表示不参与；
4. Hook 抛错默认 `FAILED`，不得静默接受，因为它属于确定性运行策略。

## 8. Completion Reviewer

### 8.1 启用方式

新增配置：

```text
TSM_AGT_COMPLETION_REVIEW_MODE=always|required|off
TSM_AGT_COMPLETION_REVIEW_MAX_CONTINUATIONS=1
TSM_AGT_COMPLETION_REVIEW_FAILURE_POLICY=visible_unverified|incomplete|accept
```

普通 Turn 默认：

```text
mode=off
max_continuations=2
failure_policy=incomplete
```

Goal/长时工作流可在独立部署或显式配置中设置 `mode=always`。测试必须分别覆盖普通 Turn 的默认关闭路径和 Reviewer 显式开启路径。

不提供 `keyword`、`source_only`、`adaptive_regex` 等模式，避免重新引入 Harness 语义分类。

### 8.2 Reviewer 请求

新增：

```python
ModelCallPurpose.COMPLETION_REVIEW
```

Reviewer ModelRequest：

```python
ModelRequest(
    turn_id=candidate.turn_id,
    messages=(system_message, review_input_message),
    tools=(),
    allow_tool_calls=False,
    require_evidence_questions=False,  # 兼容阶段；字段最终删除
    purpose=ModelCallPurpose.COMPLETION_REVIEW,
    max_output_tokens=800,
)
```

Reviewer 不接收完整会话，只接收：

- 当前原始 User Message；
- 候选 Assistant Message；
- 当前 Turn 的 `ToolExecutionSummary`；
- 当前 Turn 是否发生 mutation、是否有工具失败/截断；
- 审查次数。

Reviewer 调用不占用主 Agent 的 `max_model_calls`，否则候选回答恰好用完主预算时将永远无法审查；它使用独立的固定预算：每个候选最多 1 次物理 Reviewer 请求，Provider transport retry 仍由现有 `ResilientModel` 规则管理。Reviewer token 必须计入 Turn 总 usage 和审计事件，但不能增加 `state.model_call_count`；新增独立计数 `reviewer_call_count`。

### 8.2.1 Reviewer 故障语义

`TSM_AGT_COMPLETION_REVIEW_FAILURE_POLICY` 必须精确定义为：

- `incomplete`：Provider 超时、传输失败、非法 JSON、Schema 不匹配或 Reviewer 产生 ToolCall 时，记录 `turn.completion_review_failed`，随后返回 `AgentTurnIncomplete(reason="completion_reviewer_unavailable")`；不得提交候选回答。
- `accept`：记录同一失败事件和明显的诊断字段，然后跳过 Reviewer、继续提交候选回答。此模式只用于本地降级和紧急回滚，不得作为生产默认。
- `visible_unverified`：记录失败和 `turn.completion_review_bypassed`，随后提交已经生成的候选回答，但标记为不可作为后续模型上下文的事实回放。此为默认：完成度审查属于质量增强，不是写入授权或安全边界，不能因为审查服务不可用而吞掉用户可见回答。

Reviewer 故障不能消耗一次 `review_continuations`，因为主 Agent 尚未收到可执行的缺口反馈。

### 8.3 Reviewer System Prompt

实现必须使用以下语义，不要求逐字一致：

```text
你是一个 Turn 完成审查器，不是任务执行 Agent。
你不能调用工具，也不能提供新的业务答案。

判断候选回答是否已经满足当前用户请求：

1. 如果请求可以仅凭稳定通用知识回答，不要因为没有工具调用而拒绝。
2. 如果请求要求检查当前工作区、真实源码、指定仓库、网页、外部服务、当前状态或执行操作，
   必须在本轮工具记录中看到与结论相关的成功结果。
3. 搜索结果、仓库解析、目录列表和文件路径只能证明“发现了位置”；
   当回答声称分析具体实现时，还必须有实际文件内容、源码片段或等价内容读取结果。
4. 不要因为工具调用数量少而拒绝；只判断关键结论是否有足够输入。
5. 合法的澄清问题、权限阻塞说明、不可用错误说明，可以 ACCEPT。
6. 如果仍有一个会实质改变答案的步骤没有完成，返回 CONTINUE。
7. 不要指定必须调用哪个工具；只描述缺失事实或未完成结果。

只输出符合 Schema 的 JSON。
```

### 8.4 Reviewer 输出 Schema

```json
{
  "type": "object",
  "properties": {
    "action": {
      "type": "string",
      "enum": ["ACCEPT", "CONTINUE"]
    },
    "reason_code": {
      "type": "string",
      "minLength": 1,
      "maxLength": 64
    },
    "reason": {
      "type": "string",
      "minLength": 1,
      "maxLength": 1000
    },
    "missing": {
      "type": "array",
      "maxItems": 8,
      "items": {"type": "string", "maxLength": 300}
    }
  },
  "required": ["action", "reason_code", "reason", "missing"],
  "additionalProperties": false
}
```

### 8.5 Reviewer continuation

当 verdict 为 `CONTINUE` 时，追加 ephemeral User Message：

```json
{
  "type": "completion_review_continuation",
  "reason_code": "external_source_not_read",
  "reason": "候选回答比较了两个仓库的实现，但本轮没有读取实现文件内容。",
  "missing": [
    "读取 Codex 中与结论相关的实现内容",
    "读取 Hermes 中与结论相关的实现内容"
  ],
  "instruction": "Continue the same task. Decide the next action and tools yourself. Do not merely restate the draft answer."
}
```

新增 scaffolding kind：

```text
completion-review
```

消息必须满足：

- `role=MessageRole.USER`；
- `scaffolding="completion-review"`；
- 保存到活跃 Checkpoint 以支持进程恢复；
- 不写入最终 Session 用户消息历史；
- 候选 Assistant Message保留在当前 Turn 上下文中，使模型知道哪部分被拒绝；
- 不把 Reviewer 输出伪装成真实用户输入。

## 9. Conversation Loop 改造

修改：

```text
src/tsm_agt/core/conversation_loop/loop.py
src/tsm_agt/core/conversation_loop/finalizer.py
src/tsm_agt/core/conversation_loop/state.py
```

### 9.1 替换当前结束分支

删除：

```python
if not parsed_calls:
    if await self._request_path_only_refinement(state):
        continue
    ... persist_text_completion(...)
```

替换为：

```python
if not parsed_calls:
    candidate = await self._finalization.build_candidate(state, response)

    runtime_decision = await self._finalization.runtime_gate(
        state, response, candidate
    )
    if runtime_decision.action is CONTINUE:
        await self._finalization.continue_turn(state, runtime_decision)
        continue
    if runtime_decision.action is INCOMPLETE:
        return await self._finalizer.persist_partial_boundary(
            state, runtime_decision.reason_code
        )
    if runtime_decision.action is FAILED:
        raise TurnFinalizationFailed(runtime_decision.reason_code)

    stop_decision = await self._finalization.run_stop_policies(candidate)
    if stop_decision is not None and stop_decision.action is CONTINUE:
        if await self._finalization.can_continue(state):
            await self._finalization.append_review_continuation(
                state, stop_decision, source="stop_policy"
            )
            continue
        return await self._finalizer.persist_partial_boundary(
            state, "stop_policy_continuation_exhausted"
        )

    review = await self._finalization.review(candidate)
    if review.action is CONTINUE:
        if await self._finalization.can_continue(state):
            await self._finalization.append_review_continuation(
                state, review, source="completion_reviewer"
            )
            continue
        return await self._finalizer.persist_partial_boundary(
            state, "completion_review_exhausted"
        )

    return await self._finalizer.persist_text_completion(state, response)
```

### 9.2 TurnState 新字段

新增：

```python
review_continuations: int = 0
reviewer_call_count: int = 0
last_candidate_hash: str = ""
last_review_reason_code: str = ""
```

删除：

```python
path_only_refinement_requested
path_only_candidate_tools
```

### 9.3 重复候选保护

如果 Reviewer 返回 `CONTINUE` 后，主 Agent 再次产生内容完全相同的候选回答：

- 计算标准化文本 hash；
- 仍允许 Reviewer 再审一次；
- 第二次相同且仍为 CONTINUE 时直接 `INCOMPLETE(reason="repeated_rejected_candidate")`；
- 不无限重复相同提示。

## 10. Tool 协议简化

### 10.1 ToolCall v2

目标结构：

```python
@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    outcome_ref: str | None = None
    outcome_binding_mode: OutcomeBindingMode = OutcomeBindingMode.FULFILLMENT
```

删除：

```python
evidence_question
evidence_intent
```

如果 `evidence_intent` 仍被其他未删除能力使用，应在实施前另立 Spec；不得为了兼容 EvidenceQuestion 将它保留在主 ToolCall 协议中。

### 10.2 ToolSpec

删除属性：

```python
ToolSpec.requires_evidence_question
```

保留：

- `effect`：用于权限、执行和 mutation 语义；
- `result_authority`：若仍被 UI/审计使用可以保留，但不得再驱动 EvidenceQuestion；
- `protocol`：用于 WAIT_USER/IMMEDIATE 生命周期。

### 10.3 ModelRequest

删除：

```python
require_evidence_questions
```

OpenAI/Anthropic Adapter 直接向 Provider 暴露原始 ToolSpec 参数 Schema，不再包装为：

```json
{
  "evidence_question": {},
  "tool_arguments": {}
}
```

### 10.4 Provider 兼容读取

在一个兼容版本内，如果 Provider 返回旧 envelope：

```json
{
  "evidence_question": {...},
  "tool_arguments": {...}
}
```

Adapter 可以只提取 `tool_arguments` 并记录：

```text
tool.legacy_evidence_envelope_observed
```

不得继续生成或要求旧 envelope。下一主版本删除该兼容解析。

## 11. 重复搜索与读取策略迁移

当前以下策略依赖 question ID：

- evidence-guided exploration；
- progressive scope；
- read hits；
- artifact read；
- stop or pivot；
- semantic action classifier 的部分分支。

迁移后统一使用 `ToolCallFingerprint`：

```python
@dataclass(frozen=True, slots=True)
class ToolCallFingerprint:
    tool_name: str
    normalized_arguments_hash: str
    target_identity: str = ""
```

计算规则：

1. 参数 key 排序；
2. 去除运行时字段和空值；
3. 文件路径使用 workspace canonical path；
4. URL 去除 fragment，保留影响资源身份的 query；
5. 分页 cursor、offset、line range 必须参与 hash；
6. secret 参数先脱敏再 hash；
7. 相同工具+相同有效参数才算重复。

策略结果只能用于：

- 拒绝完全相同且无进展的重复调用；
- 提示模型改变参数或读取范围；
- 控制调用预算。

不得用于判断用户意图或 Task 是否完成。

## 12. Checkpoint v2 与历史兼容

### 12.1 新字段

`AgentTurnCheckpoint` 新增：

```python
checkpoint_schema_version: int = 2
finalization_state: Mapping[str, Any] = field(default_factory=dict)
```

`finalization_state` Schema：

```json
{
  "review_continuations": 1,
  "reviewer_call_count": 2,
  "last_candidate_hash": "sha256:...",
  "last_review_reason_code": "external_source_not_read"
}
```

### 12.2 v2 删除字段

新 Checkpoint 不再写入：

```text
evidence_question_state
completion_readiness_state
evidence_relation_state
exploration_outcome_state
```

如果 `evidence_inventory` 仅服务旧证据系统，也应删除；如果 UI 或审计仍使用它，必须改名为明确的 `tool_execution_inventory`，不得保留旧语义名称。

### 12.3 v1 读取

实现独立兼容函数：

```python
def load_checkpoint_v1(data: Mapping[str, Any]) -> AgentTurnCheckpoint:
    ...
```

读取顺序：

1. 在丢弃字段前，按 v1 canonical payload 校验旧 `checkpoint_hash`；
2. 使用旧 ToolCall parser 读取 pending calls；
3. 丢弃每个 ToolCall 的 `evidence_question/evidence_intent`；
4. 丢弃 evidence/readiness 投影状态；
5. 生成内存中的 v2 Checkpoint；
6. 第一次保存时写成 v2，不原地修改旧 RuntimeEvent。

禁止做法：先删除旧字段再使用 v2 hash 校验旧 Checkpoint。这会把合法历史数据误判为损坏。

### 12.4 Event 兼容

历史 Event 永久保留，不做数据库重写。投影器必须忽略已废弃事件：

```text
evidence.question_bound
evidence.question_observed
evidence.question_disposed
completion.readiness_evaluated
completion.continuation_requested（旧 readiness 来源）
```

## 13. 新事件与可观测性

必须新增以下事件：

```text
turn.finalization_candidate_created
turn.finalization_runtime_evaluated
turn.stop_policy_evaluated
turn.completion_review_started
turn.completion_review_completed
turn.completion_review_continuation
turn.completion_review_exhausted
turn.completion_review_failed
turn.finalization_accepted
```

事件最小字段：

```json
{
  "turn_id": "...",
  "model_call": 2,
  "tool_calls": 4,
  "candidate_hash": "sha256:...",
  "action": "CONTINUE",
  "reason_code": "external_source_not_read",
  "review_continuations": 1,
  "orchestration_mode": "CONVERSATION_LOOP"
}
```

禁止在事件中写入：

- 完整候选回答；
- 完整工具结果；
- secret；
- Reviewer 原始 prompt。

指标：

```text
completion_review_total{action,reason_code}
completion_review_latency_ms
completion_review_continuations_total
completion_review_exhausted_total
finalization_runtime_rejections_total{reason_code}
legacy_evidence_envelope_observed_total
```

## 14. 配置迁移

删除：

```text
TSM_AGT_COMPLETION_READINESS_MODE
require_evidence_questions
completion_max_stalled_continuations
```

新增：

```text
TSM_AGT_COMPLETION_REVIEW_MODE
TSM_AGT_COMPLETION_REVIEW_MAX_CONTINUATIONS
TSM_AGT_COMPLETION_REVIEW_FAILURE_POLICY
```

启动时如果检测到旧环境变量：

- 记录一次 deprecation warning；
- 不得把旧 mode 映射成新的语义行为；
- 在一个兼容版本后将旧变量视为未知配置错误。

配置快照必须记录：

```json
{
  "completion_review": {
    "mode": "off",
    "max_continuations": 2,
    "failure_policy": "incomplete"
  }
}
```

## 15. 删除清单

### 15.1 端口和 Adapter

最终删除：

```text
src/tsm_agt/ports/completion_readiness.py
src/tsm_agt/adapters/rule_based_completion_readiness/
```

从 `ports/__init__.py` 删除全部 CompletionReadiness 和 EvidenceQuestion 导出。

### 15.2 Core

最终删除：

```text
src/tsm_agt/core/evidence_question.py
ConversationLoop._request_path_only_refinement
Kernel._evaluate_completion_readiness
Kernel._bind_evidence_question
Kernel._observe_evidence_question
Kernel._dispose_evidence_question
```

删除 Checkpoint、Working Memory、Session Context 中的 evidence question 投影。

### 15.3 Model Adapter

删除 OpenAI/Anthropic Adapter 中：

- evidence envelope Schema 包装；
- automatic evidence question；
- missing/invalid evidence question protocol error；
- ToolCall evidence question serialization。

### 15.4 旧编排路径

在 Conversation Loop 验收通过后删除：

```text
src/tsm_agt/core/single_loop/
LEGACY orchestration 分支
SINGLE_LOOP orchestration 配置
single_loop_allow_fallback
```

可复用的通用类型必须先迁移到非 `single_loop` 包；不得让 `CONVERSATION_LOOP` 反向依赖被删除目录。

## 16. 分阶段实施计划

### Phase 0：建立基线

交付：

- 固定当前 Conversation Loop 集成测试结果；
- 增加真实问题回归 Fixture：`对比 codex 和 hermes 源码中的工具循环实现`；
- 记录当前工具 Schema 快照和平均 token 数；
- 记录历史 Checkpoint Fixture。

完成条件：基线可重复，且现有失败场景能稳定复现为“零工具或只发现路径仍完成”。

### Phase 1：引入 Finalization Pipeline

交付：

- `turn_finalization.py` 端口；
- Runtime Finalization Gate；
- Completion Reviewer Adapter；
- 新配置和事件；
- Conversation Loop 接入；
- 旧 readiness 仅保留编译，不再参与 Conversation Loop。

完成条件：新回归场景第一次候选被 Reviewer 拒绝，主 Agent 获得 continuation 并继续调用工具。

### Phase 2：简化 Tool Schema

交付：

- `require_evidence_questions=False` 成为所有运行路径事实；
- Provider 直接暴露原始 Tool 参数；
- ToolCall v2；
- 旧 envelope 只读兼容；
- 基于 Fingerprint 的重复调用保护。

完成条件：模型请求 Schema 中不存在 `evidence_question` 和 `tool_arguments` 包装。

### Phase 3：Checkpoint v2

交付：

- v1 hash 校验和 lazy upgrade；
- v2 finalization state；
- 恢复中断 Reviewer continuation；
- 新 Checkpoint 不写旧状态。

完成条件：历史 SQLite Fixture 可恢复；新 Checkpoint round-trip hash 稳定。

### Phase 4：物理删除旧系统

交付：

- 删除 EvidenceQuestion/CompletionReadiness 代码、配置、测试；
- 删除 SINGLE_LOOP/LEGACY；
- 更新 README、`.env.example` 和架构测试。

完成条件：

```bash
rg -n "EvidenceQuestion|evidence_question|CompletionReadiness|completion_readiness" src
```

除历史兼容 reader 和 migration 注释外无匹配；兼容 reader 必须集中在单个模块。

### Phase 5：生产观测与清理兼容层

交付：

- 观察 Reviewer 继续率、耗尽率、误拒绝率；
- 对真实失败样本增加 Eval；
- 下一主版本删除旧 Tool envelope 和 Checkpoint v1 parser。

## 17. 测试计划

### 17.1 Runtime Gate 单元测试

必须覆盖：

1. `STOP + 非空文本 + 无 pending` → Runtime ACCEPT。
2. `LENGTH + 有 budget` → CONTINUE。
3. `LENGTH + 无 budget` → INCOMPLETE。
4. pending tool calls → FAILED。
5. pending ToolBatch → FAILED。
6. pending approval/user action → suspension，不完成。
7. unmatched ToolCall/ToolResult → FAILED。
8. 空回答有 budget → CONTINUE。
9. 空回答无 budget → INCOMPLETE。

### 17.2 Reviewer 单元测试

使用 Fixture Model 覆盖：

1. 普通知识问题、零工具 → ACCEPT。
2. 源码对比、零工具 → CONTINUE。
3. 源码对比、只有 `resolve/list_tree/search_code` 路径 → CONTINUE。
4. 两侧都有实际源码内容结果 → ACCEPT。
5. 请求检查当前工作区、零工具 → CONTINUE。
6. 合法权限阻塞说明 → ACCEPT。
7. Reviewer 非法 JSON → 按 failure policy 处理。
8. Reviewer 企图 tool call → protocol failure。

### 17.3 Conversation Loop 集成测试

必须覆盖：

```text
User asks source comparison
→ Agent draft without tools
→ Reviewer CONTINUE
→ ephemeral completion-review message
→ Agent calls source tools
→ tool results returned
→ Agent final draft
→ Reviewer ACCEPT
→ exactly one durable assistant final response
```

另外覆盖：

- Reviewer 连续 CONTINUE 达上限 → `AgentTurnIncomplete`；
- 相同候选重复 → bounded incomplete；
- Reviewer continuation 后进程重启 → 从 Checkpoint 恢复；
- continuation scaffolding 不进入下一用户 Turn；
- approval/clarification 路径不调用 Reviewer；
- 用户 steering 到达时优先处理 steering；
- 工具结果截断会出现在 Reviewer summary，但 Runtime 不自行推断是否足够。

### 17.4 兼容测试

- v1 ToolCall 含 EvidenceQuestion 可读取，运行时对象不保留它；
- v1 Checkpoint 先按旧 payload 验 hash，再升级；
- v1 pending tool batch 可恢复并执行；
- 历史 Event 投影不会重建旧 evidence state；
- v2 Checkpoint 不输出任何旧字段。

### 17.5 架构测试

增加静态约束：

- `conversation_loop` 不得 import `evidence_question` 或 `completion_readiness`；
- Model Adapter 不得生成 `evidence_question` Schema；
- Completion Reviewer 不得获得 tools；
- Runtime Finalization Gate 不得 import 语义 classifier；
- 只有 `CONVERSATION_LOOP` 可作为普通 Turn 编排模式。

## 18. 验收标准

全部满足才可视为迁移完成：

### AC-1 主路径

所有普通用户输入进入 `CONVERSATION_LOOP`，不存在工具不可用的 ANSWER 快速路径。

### AC-2 工具协议

模型可见的所有工具 Schema 都是原始参数 Schema，不含 EvidenceQuestion envelope。

### AC-3 源码比较回归

输入：

```text
对比 Codex 和 Hermes 源码中的工具调用循环，给出具体实现差异。
```

当主 Agent 第一次零工具回答时：

- Turn 不得完成；
- Reviewer 返回 CONTINUE；
- 主 Agent 获得非工具指定型缺口提示；
- 在有可用工具且无外部阻塞时，后续至少出现与两个目标相关的真实内容读取结果；
- 最终只保存一个用户可见 assistant final response。

### AC-4 普通知识问题

不要求所有回答调用工具。稳定通用知识问题允许零工具完成。

### AC-5 无 Harness 意图分类

代码中不存在基于用户关键词决定是否调用源码/Web/工作区工具的规则。

### AC-6 有界失败

Reviewer 无法被满足时，最多继续默认 2 次，随后明确返回 INCOMPLETE，不死循环、不虚假完成。

### AC-7 恢复

在以下任一点重启均能恢复：

- 工具批次执行前；
- 工具结果持久化后；
- Reviewer CONTINUE 写入后；
- 下一次主模型调用前。

### AC-8 旧数据

现有 Runtime DB 中至少一份真实历史 Checkpoint 能通过 v1 reader 恢复并继续。

### AC-9 回归

审批、取消、澄清、多轮会话、工具错误纠正、context compaction 和 streaming 测试全部通过。

## 19. 发布与回滚

### 19.1 发布顺序

1. 先发布 Phase 1，保留旧代码但 Conversation Loop 不再调用；
2. 开启 Reviewer shadow 观测一轮：执行审查但不阻断，只记录 verdict；
3. 对已知源码比较 Eval 达标后切换 `failure_policy=incomplete` 并实际执行 CONTINUE；
4. 完成 Phase 2/3；
5. 最后物理删除旧系统。

Shadow 只允许作为短期发布步骤，不作为长期第三种运行模式进入公共配置。

### 19.2 回滚

Phase 1–3 回滚开关：

```text
TSM_AGT_COMPLETION_REVIEW_MODE=off
```

回滚只关闭模型 Reviewer，不关闭 Runtime Finalization Gate。Runtime Gate 属于协议正确性，不得通过配置绕过。

物理删除旧系统后，不回滚到 EvidenceQuestion/CompletionReadiness；只能修复新 Finalization Pipeline 或暂时关闭 Reviewer。

## 20. 实施完成定义

实施者提交时必须同时提供：

- 新旧架构差异说明；
- 删除文件和新增文件列表；
- Tool Schema 前后快照；
- 历史 Checkpoint 升级测试证据；
- 源码比较、普通知识、审批恢复三条端到端日志；
- Reviewer token/latency 数据；
- 全量测试结果；
- `rg` 旧符号清理结果。

缺少其中任一项，不得声明迁移完成。
