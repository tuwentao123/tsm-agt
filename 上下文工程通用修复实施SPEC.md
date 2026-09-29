# 上下文工程通用修复实施 SPEC

> 上游依据：`上下文工程通用修复结论.md`（缺陷 D1–D3、修复 Fix-1–5、不变量 INV-8–12）。
> 本文件是**可直接执行**的实施说明：每个阶段给出精确文件、函数签名、行为、事件、测试、验收命令、回滚方式。
> 术语中英对照：来源 = provenance；作用域 = scope；权威档位 = authority tier；判据 = criterion；裁判 = judge。

**状态**：已实施完成（P0–P5 全部落地）。
**基线**：全量测试 `1111 passed / 9 failed / 8 skipped`，9 个失败为既有失败（见 §8）。
**实施后**：全量测试 `1144 passed / 9 failed / 8 skipped`，失败集合与基线完全一致。
**运行环境**：`.venv/bin/python`（3.12）；工作区 `/Users/tusima/Documents/学习/tsm-agt`。

---

## 1. 目标 / 非目标

### 1.1 目标（对任意输入成立）

| 编号 | 不变式 | 落地阶段 |
|---|---|---|
| INV-8 | Planner 上下文中不存在"既不属于当前 task 谱系、也未被显式引用"的消息 | Phase 1 / 5 |
| INV-9 | 进入 TaskSpec（goal / 判据）的文本只能来自 `AUTHORITATIVE` | Phase 2 |
| INV-10 | 存在 `rubric` / `goal_alignment` 判据 ⇒ 必须有可用 judge；否则终态 `NEEDS_REVIEW` | Phase 3 |
| INV-11 | 含 `ANSWER` outcome 的提案必须带一条 `goal_alignment` 判据 | Phase 3 |
| INV-12 | 续跑必须由显式 task_id 或唯一候选确定；多候选 ⇒ `CLARIFY` | Phase 4 |
| — | 压缩的"资格"改为 provenance，保留 pinned；回执带被遗忘事件序列 | Phase 5 |

### 1.2 非目标（明确不做）

- 不做关键词过滤（如"鸡肉/无关话题"黑名单）。
- 不按任务类型收紧上下文（不做"UI 类任务特殊处理"）。
- 不调大 `recent_visible_message_limit` 充当修复。
- 不引入话题相似度（embedding）选择。
- 不压缩 append-only 事件库。

---

## 2. 现状 → 目标流程差异（唯一权威表）

| # | 阶段 | 现状函数/位置 | 现状行为 | 目标行为 | 阶段 |
|---|---|---|---|---|---|
| 1 | Write | `SessionContextProjector.project()` `session_context.py:520+` | 事件 append-only | 不变 | — |
| 2 | 路由 | `kernel.py:1820-2010` | 上下文依赖 + 无源 ⇒ 裸建 `CONTEXTUAL` task，goal = 用户原文 | 唯一候选 ⇒ 定向 `FOLLOW_UP`；多候选 ⇒ `CLARIFY`；零候选才 `CONTEXTUAL` | P4 |
| 3 | Select(goal) | `session_handoff.build_session_follow_up_goal` | goal = 当前请求 ⊕ 源任务剩余工作（同一字符串） | goal = 当前请求；源任务拆到 `background_task` 字段 | P2 |
| 4 | Planner | `kernel._task_spec_planning_context` `kernel.py:2849-2925` | `session.recent_messages` = 全局最近 8 条 | `scope=ContextScope(lineage=当前 task)`、`include_background=False` | P1 |
| 5 | Planner 输出 | `kernel.plan_task_spec` `kernel.py:2969-3027` | 背景文本可变成判据；无对齐判据 | 背景泄漏判据降级 + 事件；含 ANSWER 时追加 `goal_alignment` | P2/P3 |
| 6 | Executor | `_project_context_messages` `kernel.py:6232-6249` | `session-context-…` 无权威标注 | 每条消息带 `authority`；payload 带 `authority` 分档表 | P1 |
| 7 | Compress | `ContextWindowManager._compact_session_messages` `context.py:361-432` | 摘要资格 = "最近 8 条涉及 task"，丢 pinned | 资格 = 最近 ∪ pinned；回执带 `forgotten_event_sequences` | P5 |
| 8 | Isolate | `generated_prefixes` 重建 `kernel.py:12462-12475` | 已正确 | 不变（补测试） | P0 |
| 9 | Verify | `verify_task_acceptance` `kernel.py:8811-9245` | rubric 无 judge ⇒ 静默 `continue` | 无 judge ⇒ `BLOCKED`；`BLOCKED` ⇒ `NEEDS_REVIEW`；judge 收到证据正文 | P3 |

---

## 3. 新增 / 修改的数据契约

### 3.1 `ContextAuthority`（新，`core/session_context.py`）

```python
class ContextAuthority(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"          # 用户当前输入、显式引用、当前 TaskSpec
    SCOPED_BACKGROUND = "SCOPED_BACKGROUND"  # 显式指名源任务的身份/状态（有界）
    NON_AUTHORITATIVE = "NON_AUTHORITATIVE"  # 会话历史、其它任务、摘要、子代理报告
```

### 3.2 `ContextScope` + 选择器（新，`core/session_context.py`）

```python
@dataclass(frozen=True, slots=True)
class ContextScope:
    session_id: str
    task_id: str | None = None
    lineage_task_ids: tuple[str, ...] = ()
    explicit_message_ids: tuple[str, ...] = ()
    explicit_event_sequences: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("ContextScope requires a session id")

    @property
    def eligible_task_ids(self) -> frozenset[str]:
        return frozenset(self.lineage_task_ids)

    def allows(self, message: SessionConversationMessage) -> bool:
        if message.message_id in self.explicit_message_ids:
            return True
        if message.source_event_sequence in self.explicit_event_sequences:
            return True
        return (
            message.task_id is not None
            and message.task_id in self.eligible_task_ids
        )


def select_messages_by_scope(
    messages: Sequence[SessionConversationMessage], scope: ContextScope,
) -> tuple[SessionConversationMessage, ...]:
    """Provenance selector.  Recency is never an eligibility criterion."""
    return tuple(m for m in messages if scope.allows(m))
```

**注意**：`task_id is None` 的消息默认**不入选**（无来源即无权威）。若迁移期需兼容，由 `include_unattributed=False` 显式控制，默认 False。

### 3.3 `SessionFollowUpHandoff` + 背景泄漏检测（新，`core/session_handoff.py`）

```python
@dataclass(frozen=True, slots=True)
class SessionFollowUpHandoff:
    goal: str                                  # AUTHORITATIVE，仅当前请求
    background_task: Mapping[str, Any]         # SCOPED_BACKGROUND，有界
    background_texts: tuple[str, ...]          # 供泄漏检测使用的规范化前文本

_BACKGROUND_SPAN_MIN = 24

def find_background_leak(
    authored_text: str, background_texts: Sequence[str], *,
    min_span: int = _BACKGROUND_SPAN_MIN,
) -> str | None:
    """返回第一个"背景原文中长度 ≥ min_span 的片段逐字出现在 authored_text 中"的片段。

    确定性纯函数：casefold + 折叠空白后做双向子串扫描。
    """
```

### 3.4 `RubricEvidence` + 判官端口 v2（`ports/rubric_judge.py`）

```python
@dataclass(frozen=True, slots=True)
class RubricEvidence:
    reference: str        # event:123 / tool_call:abc / mutation:x
    excerpt: str          # 有界正文，≤ rubric_evidence_excerpt_characters

class RubricJudgePort(RuntimeAdapter, Protocol):
    async def judge(
        self, criterion_id: str, assertion: str,
        evidence: tuple[RubricEvidence, ...],
    ) -> RubricJudgement: ...
```

端口契约版本 `1.0 → 2.0`；`ModelRubricJudge.descriptor` 同步升级。
**动机（现状缺陷）**：现签名只传 `evidence_refs`（如 `["event:123"]`），judge 看不到正文，
即使装配了 judge 也是盲判——`goal_alignment` 无法工作。

### 3.5 `TaskCriterionKind.GOAL_ALIGNMENT`（`core/task_spec.py`）

```python
class TaskCriterionKind(StrEnum):
    WORKSPACE_INTEGRITY = "workspace_integrity"
    POST_MUTATION_COMMAND = "post_mutation_command"
    EVIDENCE_REFERENCE = "evidence_reference"
    RUBRIC = "rubric"
    GOAL_ALIGNMENT = "goal_alignment"   # 新增：Runtime 追加，Planner 不得撰写
```

**Schema 必须排除它**（`TASK_SPEC_PROPOSAL_SCHEMA_V1`，`task_spec.py:657`）：

```python
"enum": [
    item.value for item in TaskCriterionKind
    if item is not TaskCriterionKind.GOAL_ALIGNMENT
],
```

`TaskAcceptanceCriterion.__post_init__` 已禁止非 `EVIDENCE_REFERENCE` 携带 reference，无需改动。
`validate_authored_reference` 只校验 `EVIDENCE_REFERENCE`，无需改动。

### 3.6 `ContextCompactionReceipt` 扩展（`core/context.py:68-101`）

```python
forgotten_message_ids: tuple[str, ...] = ()
forgotten_event_sequences: tuple[int, ...] = ()
pinned_message_ids: tuple[str, ...] = ()
```

`event_data()` 增加对应三个键（`forgotten_event_sequences` 为 list[int]）。

---

## 4. 实施阶段

> 每个阶段独立可测、可回滚。**顺序 P0 → P1 → P2 → P3 → P4 → P5**；P3 与 P1/P2 无代码耦合，可并行。

### Phase 0 — 基线固化（零行为变更）

**目标**：把现状写成断言，使后续改动可回归。

1. 新增 `tests/test_context_scope_selection.py`（P1 用）与 `tests/test_authority_layering.py`（P2 用）骨架，
   先只断言**现状**行为（`for_prompt` 无 scope 时仍取最近 8 条）。
2. 新增观测：`_task_spec_planning_context` 返回值不变，但在 `task_spec.revised` 事件 payload 中
   记录 `scoped_message_count` / `background_message_count` / `dropped_message_count`。
3. 记录基线：

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

**验收**：输出与 §8 基线一致（1111 passed / 9 failed）。
**回滚**：删除新增测试与事件字段。

---

### Phase 1 — INV-8：Planner 按 provenance 选择

**改动文件**：`src/tsm_agt/core/session_context.py`、`src/tsm_agt/core/kernel.py`、`tests/test_session_context.py`

#### 1.1 `SessionContextProjector` 新字段

```python
scoped_visible_message_limit: int = 32      # 新：scope 内的资格上限（仅用于封顶）
recent_visible_message_limit: int = 8       # 保留：background 的 recency 上限
```

`__post_init__` 的正数校验加入 `scoped_visible_message_limit`。

#### 1.2 `for_prompt` 签名与行为

```python
def for_prompt(
    self, projection: SessionConversationProjection, *,
    recent_executions: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    active_checkpoint: SessionActiveCheckpoint | None = None,
    suspended_tasks: Sequence[SessionResumeCandidate] = (),
    pinned_task_ids: Sequence[str] = (),
    scope: ContextScope | None = None,        # 新
    include_background: bool = True,          # 新
) -> SessionPromptProjection:
```

选择逻辑（替换现有 `visible_messages = prompt_messages[-limit:]`）：

```python
if scope is None:
    scoped_messages: tuple[SessionConversationMessage, ...] = ()
    background_messages = prompt_messages[-self.recent_visible_message_limit:]
else:
    in_scope = select_messages_by_scope(prompt_messages, scope)
    scoped_messages = in_scope[-self.scoped_visible_message_limit:]
    scoped_ids = {m.message_id for m in scoped_messages}
    background_messages = tuple(
        m for m in prompt_messages if m.message_id not in scoped_ids
    )
    if not include_background:
        background_messages = ()
    else:
        background_messages = background_messages[-self.recent_visible_message_limit:]
visible_messages = scoped_messages + background_messages
```

`visible_messages` 仍驱动 `recent_task_ids` / `prompt_task_ids` / `visible_sequences` / `earlier_summary`，
**这些下游逻辑不改**（只改"谁进来"）。

#### 1.3 `body_data` 新增键（`session_context.py:810-871`）

```python
"scope": {
    "session_id": projection.session_id,
    "task_id": scope.task_id if scope else None,
    "lineage_task_ids": list(scope.lineage_task_ids) if scope else [],
},
"authority": {
    "scoped_messages": ContextAuthority.AUTHORITATIVE.value,
    "background_messages": ContextAuthority.NON_AUTHORITATIVE.value,
    "working_state": ContextAuthority.NON_AUTHORITATIVE.value,
    "recent_task_summaries": ContextAuthority.NON_AUTHORITATIVE.value,
    "historical_investigation": ContextAuthority.NON_AUTHORITATIVE.value,
    "task_index": ContextAuthority.NON_AUTHORITATIVE.value,
},
"pinned_task_ids": list(pinned_ids),                     # P5 依赖
"scoped_messages": [
    {**m.source_data(), "authority": ContextAuthority.AUTHORITATIVE.value}
    for m in scoped_messages
],
"background_messages": [
    {**m.source_data(), "authority": ContextAuthority.NON_AUTHORITATIVE.value}
    for m in background_messages
],
"recent_messages": [m.source_data() for m in visible_messages],   # 兼容别名，保留
```

空投影分支（`session_context.py:706-725`）同步补 `scoped_messages` / `background_messages` / `authority` 空值。

#### 1.4 `kernel._task_spec_planning_context`（`kernel.py:2869-2918`）

```python
scope = ContextScope(
    session_id=task.session_id,
    task_id=task.task_id,
    lineage_task_ids=(task.task_id,),
)
prompt_projection = self._dependencies.session_context_projector.for_prompt(
    projection,
    pinned_task_ids=((source_task_id,) if source_task_id else ()),
    scope=scope,
    include_background=False,          # Planner 不收任何非权威文本
)
...
planning_context = {
    "workspace": task.workspace,
    "available_tool_effects": sorted({...}),
    "authority": dict(prompt_data.get("authority", {})),
    "session": {
        "revision": prompt_projection.revision,
        "working_state": prompt_data.get("work_state", {}),
        "scoped_messages": prompt_data.get("scoped_messages", []),
        "background_messages": prompt_data.get("background_messages", []),  # 恒为 []
        "recent_artifacts": [...],          # 保留原样
    },
    "related_task": related_task,           # P2 加 authority 标注
}
```

**删除**：`"recent_messages": prompt_data.get("recent_messages", [])`。

#### 1.5 `related_task` 增加权威标注（`kernel.py:2886-2894`）

```python
related_task = {
    "task_id": ...,
    "state": ...,
    "verification_status": ...,
    "goal": ...,
    "historical_remaining_work": ...,
    "completed_work": ...,
    "mutations": ...,
    "authority": ContextAuthority.SCOPED_BACKGROUND.value,
}
```

#### 1.6 Planner adapter prompt（`adapters/model_task_spec_planner/planner.py:117-125`）

把自然语言说明替换为**按档位渲染的结构化输入**：`runtime_context.authority` 原样进入 user JSON；
system 中"哪一档可以写 goal/判据"只保留一句，作为数据结构之上的说明而非唯一防线：

```
"Only content tagged AUTHORITATIVE in runtime_context.authority may become "
"the goal or an acceptance criterion. SCOPED_BACKGROUND may inform planning "
"but must never be copied into a criterion. Content absent from the context "
"does not exist."
```

#### 1.7 测试

`tests/test_context_scope_selection.py`（新）：

```python
def test_scope_excludes_other_task_messages() -> None:
    # 构造会话：task A 3 条消息，task B 2 条消息
    # scope(task B) 的选择结果必须不含任何 task A 消息

def test_scope_includes_explicit_reference_message() -> None:
    # 通过 explicit_event_sequences 显式引入 task A 的一条消息

def test_unattributed_message_is_not_selected() -> None:
    # task_id=None 默认不入选

def test_planner_context_has_zero_unrelated_messages() -> None:
    # 经 Kernel._task_spec_planning_context 断言：
    # planning_context["session"]["scoped_messages"] 全部 task_id == 当前 task
    # planning_context["session"]["background_messages"] == []

def test_property_selection_is_provenance_only() -> None:
    # 会话中 N=10 个互不相关任务；对每个 task 断言零跨任务消息（循环，确定性）
```

`tests/test_session_context.py` 追加：

```python
def test_legacy_for_prompt_without_scope_is_unchanged() -> None: ...   # 无 scope 时行为冻结
def test_for_prompt_emits_authority_per_message() -> None: ...        # 每档标注存在
def test_background_messages_excluded_when_include_background_false() -> None: ...
```

**验收**：

```bash
.venv/bin/python -m pytest tests/test_context_scope_selection.py tests/test_session_context.py -q
.venv/bin/python -m pytest -q 2>&1 | tail -5      # 仍为 1111 passed / 9 failed
```

**回滚**：`for_prompt` 的 `scope` 默认 `None`，因此删除 kernel 侧的 `scope=...`/`include_background=False` 两个实参即可恢复现状。

---

### Phase 2 — INV-9：goal 纯化 + 权威分层 + 背景泄漏护栏

**改动文件**：`src/tsm_agt/core/session_handoff.py`、`src/tsm_agt/core/kernel.py`、`tests/test_session_handoff.py`

#### 2.1 `session_handoff.py`

```python
def build_session_follow_up_handoff(
    current_input: str, source: SessionTaskCatalogEntry,
) -> SessionFollowUpHandoff:
    request = _clip(current_input, _MAX_GOAL_CHARACTERS)   # 2000
    background_task = {
        "task_id": source.task_id,
        "state": source.task_state,
        "phase": source.phase1_state or "unknown",
        "verification": source.verification_status or "unknown",
        "goal": _clip(source.goal, 320),
        "historical_remaining_work": [
            _clip(v, 140) for v in source.remaining_work[:4] if v.strip()
        ],
        "completed_work": [
            _clip(v, 110) for v in source.completed_work[:3] if v.strip()
        ],
        "authority": ContextAuthority.SCOPED_BACKGROUND.value,
    }
    background_texts = tuple(
        text for text in (
            background_task["goal"],
            *background_task["historical_remaining_work"],
            *background_task["completed_work"],
        ) if text
    )
    return SessionFollowUpHandoff(request, background_task, background_texts)


def build_session_follow_up_goal(
    current_input: str, source: SessionTaskCatalogEntry,
) -> str:
    """Backward-compatible facade: the goal is ONLY the user's current request."""
    return build_session_follow_up_handoff(current_input, source).goal
```

**行为变更**：goal 不再包含 `[session-follow-up]` 头、`Authority-free source Task:` 段与源任务剩余工作。
现有断言复合字符串的测试需同步更新（`tests/test_session_handoff.py`、`tests/test_sdk_runtime.py`）。

**同时**：把现有私有 `_clip(value, limit)` 提为公开 `clip_text(value, limit)`（`_clip = clip_text` 保留别名），
供 `kernel.plan_task_spec` 的 goal 截断复用（Kernel 内目前没有等价的截断助手）。

#### 2.2 Kernel 调用点（`kernel.py:1894-1896`、`1916-1918`）

```python
handoff = build_session_follow_up_handoff(normalized, source)
resolved_goal = handoff.goal
context_handoff = handoff.background_task      # 随 task.created payload 持久化（非权威）
```

`Kernel.create_task`（`kernel.py:4389`）与 `EngineeringAgentClient.submit_task` /
`EngineeringAgentClient.create_task`（`sdk/runtime.py:283+`）增加可选参数：

```python
context_handoff: Mapping[str, Any] | None = None,
```

并把它写入 `task.created` 事件 payload 的 `context_handoff` 字段
（`kernel.py:4543-4553`，只读、`authority=SCOPED_BACKGROUND`）。
`_task_spec_planning_context` 优先从该 payload 读取 `related_task`；找不到时回退到现有
`recent_task_summaries` 路径（保证历史任务仍可规划）。

#### 2.3 背景泄漏护栏（`kernel.py`，新增私有方法）

```python
def _demote_background_leaks(
    self, criteria: tuple[TaskAcceptanceCriterion, ...],
    planning_context: Mapping[str, Any],
) -> tuple[tuple[TaskAcceptanceCriterion, ...], list[dict[str, str]]]:
    """INV-9: a criterion copied verbatim from SCOPED_BACKGROUND is demoted to advisory."""
```

`background_texts` 来源：
- `planning_context["related_task"]` 的 `goal` / `historical_remaining_work` / `completed_work`；
- `planning_context["session"]["working_state"]` 的各槽位字符串（NON_AUTHORITATIVE）。

命中 ⇒ 该判据改为 `TaskCriterionKind.RUBRIC`（advisory，仍需 judge 才会被评估），
并追加事件：

```python
"task_spec.background_leak_demoted", {
    "writer": "task-spec-planner",
    "demoted": [{"criterion_id": ..., "leaked_span": ..., "source": "SCOPED_BACKGROUND"}],
}
```

在 `plan_task_spec`（`kernel.py:2998-3008`）里紧跟 `_demote_unresolvable_criteria` 之后调用。

#### 2.4 测试

`tests/test_authority_layering.py`（新）：

```python
def test_follow_up_goal_contains_only_current_request() -> None:
    # source.remaining_work 含 "apply UI styling"; current_input="继续啊"
    # 断言 handoff.goal == "继续啊"（不含 UI styling / 不含 source goal）

def test_background_task_is_labeled_scoped_background() -> None: ...
def test_related_task_carries_authority_tag() -> None: ...
def test_criterion_verbatim_from_background_is_demoted() -> None: ...
def test_criterion_from_user_request_survives() -> None: ...
def test_leak_demotion_emits_event() -> None: ...
```

`tests/test_session_handoff.py` 追加：`build_session_follow_up_goal` 返回当前请求的守门断言。

**验收**：

```bash
.venv/bin/python -m pytest tests/test_authority_layering.py tests/test_session_handoff.py tests/test_sdk_runtime.py -q
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

**回滚**：恢复 `build_session_follow_up_goal` 原实现并移除 `_demote_background_leaks` 调用。

---

### Phase 3 — INV-10 / INV-11：裁判闭环 + 切题判据

**改动文件**：`src/tsm_agt/ports/rubric_judge.py`、`src/tsm_agt/adapters/model_rubric_judge/judge.py`、
`src/tsm_agt/core/task_spec.py`、`src/tsm_agt/core/kernel.py`、`src/tsm_agt/bootstrap/composition.py`、
`src/tsm_agt/sdk/runtime.py`

#### 3.1 端口 v2（`ports/rubric_judge.py`）

按 §3.4 增加 `RubricEvidence` 并改 `judge` 签名；`RubricJudgePort` 描述符契约版本升 `2.0`。
`ports/__init__.py` 导出 `RubricEvidence`。

#### 3.2 `ModelRubricJudge`（`judge.py`）

```python
async def judge(self, criterion_id, assertion, evidence: tuple[RubricEvidence, ...]):
    ...
    payload = {
        "criterion_id": criterion_id,
        "criterion": assertion,
        "evidence": [{"reference": e.reference, "excerpt": e.excerpt} for e in evidence],
    }
```

其余（严格 JSON、`satisfied|needs_revision|undecidable`、错误 ⇒ `JUDGE_ERROR`）不变。

#### 3.3 证据正文解析（`kernel.py`，新增）

```python
def _rubric_evidence(
    self, task: TaskSnapshot, events: tuple[RuntimeEvent, ...],
    references: tuple[str, ...],
) -> tuple[RubricEvidence, ...]:
    """把 evidence reference 解析为有界正文，供 judge 阅读。"""
```

规则：
- `event:<seq>` → 取该事件；若 `llm.completed`，提取 `payload["message"]["content"][*]["text"]`；
  否则取 `json.dumps(payload)[:limit]`。
- `tool_call:<id>` → 已提交成功结果的文本摘要（`execution.result.data` 截断）。
- `mutation:<id>` → `path` + `before_hash`/`after_hash`。
- 上限：`rubric_evidence_max_items = 4`、每条 `rubric_evidence_excerpt_characters = 800`。

`KernelDependencies` 新增两个字段（默认值如上）。

`_judge_rubric_criterion`（`kernel.py:9621-9651`）改为：

```python
evidence = self._rubric_evidence(task, events, references)
judgement = await judge.judge(
    criterion.criterion_id, criterion.description, evidence,
)
```

#### 3.4 无 judge ⇒ 不得 PASS（`kernel.py:9142-9163`）

```python
elif criterion.verification_kind in {
    TaskCriterionKind.RUBRIC, TaskCriterionKind.GOAL_ALIGNMENT,
}:
    if self._dependencies.rubric_judge is None:
        criteria.append(AcceptanceResult(
            criterion.criterion_id, AcceptanceStatus.BLOCKED,
            (Evidence(
                "rubric_unavailable", criterion.description,
                "no rubric judge is configured; a judged criterion cannot pass",
                "rubric-judge", False,
            ),),
        ))
        continue
    judgement = await self._judge_rubric_criterion(
        task_id, criterion, answer_reference, shared_observation_refs,
    )
    passed = judgement.verdict is JudgeVerdict.SATISFIED
    criteria.append(AcceptanceResult(
        criterion.criterion_id,
        AcceptanceStatus.PASSED if passed else AcceptanceStatus.BLOCKED,
        (Evidence("rubric", criterion.description, judgement.reason,
                  "rubric-judge", passed),),
    ))
```

#### 3.5 `BLOCKED ⇒ NEEDS_REVIEW`（`sdk/runtime.py:1240-1244`）

`BLOCKED` 是"无法判定"，不是"失败"（INV-6 三值）：

```python
if verification.passed:
    ... → SUCCEEDED
elif verification.status is AcceptanceStatus.BLOCKED:
    ... transition_task(..., TaskState.NEEDS_REVIEW, "SDK verifier blocked")
else:
    ... → FAILED
```

`AcceptanceStatus` 需从 `tsm_agt.core` 导入。

逐回合（非 legacy）模式下，在 readiness 评估结束前增加 fail-closed 检查：

```python
if (
    self._dependencies.rubric_judge is None
    and any(
        c.verification_kind in {
            TaskCriterionKind.RUBRIC, TaskCriterionKind.GOAL_ALIGNMENT,
        }
        for c in task_spec.acceptance_criteria
    )
):
    return await self._finalize_needs_review(
        checkpoint, gaps=(), reason="judged_criterion_without_judge",
        stalled=..., message=...,
    )
```

#### 3.6 判据分支覆盖 `GOAL_ALIGNMENT`

- `_completion_readiness_gaps`（`kernel.py:7604`）：`elif criterion.verification_kind in {RUBRIC, GOAL_ALIGNMENT}: continue`
  （逐回合 advisory，最终验收才评估）。
- `verify_task_acceptance`：`GOAL_ALIGNMENT` 走与 `RUBRIC` 相同的 judge 分支（§3.4）。

#### 3.7 Runtime 追加对齐判据（`plan_task_spec`，`kernel.py:3009-3021`）

```python
if (
    any(o.kind is TaskOutcomeKind.ANSWER for o in proposal.outcomes)
    and not any(
        c.verification_kind is TaskCriterionKind.GOAL_ALIGNMENT
        for c in proposal.acceptance_criteria
    )
):
    alignment = TaskAcceptanceCriterion(
        criterion_id="answer-alignment",
        description=(
            "The delivered answer directly addresses the user's current goal: "
            + clip_text(current_request, 400)
        ),
        verification_kind=TaskCriterionKind.GOAL_ALIGNMENT,
    )
    proposal = replace(
        proposal,
        acceptance_criteria=proposal.acceptance_criteria + (alignment,),
    )
    await self._append_events(task_id, (("task_spec.alignment_criterion_added", {
        "criterion_id": "answer-alignment",
        "goal_hash": canonical_hash(current_request),
    }),))
```

#### 3.8 生产装配（`bootstrap/composition.py`）

在 `ModelProviderPort` 注册之后（约 `composition.py:834` 与 `:992`）：

```python
registry.register(
    RubricJudgePort, ModelRubricJudge(registry.require(ModelProviderPort))
)
```

`_kernel_dependencies` 已有 `rubric_judge=(rubric_judges[0] if rubric_judges else None)`（`composition.py:421`），
无需改动；显式 `rubric_judge_adapter=` 路径保留（用于注入替身）。

导入：`from tsm_agt.adapters.model_rubric_judge import ModelRubricJudge`、`from tsm_agt.ports import RubricJudgePort`。

#### 3.9 测试

`tests/test_rubric_judge.py` 追加：

```python
def test_judge_receives_evidence_excerpt_not_only_reference() -> None: ...
def test_judged_criterion_without_judge_is_blocked_not_skipped() -> None: ...
def test_blocked_verification_maps_to_needs_review() -> None: ...
```

新增 `tests/test_goal_alignment.py`：

```python
def test_answer_task_gets_goal_alignment_criterion() -> None: ...
def test_non_answer_task_gets_no_alignment_criterion() -> None: ...
def test_alignment_criterion_absent_from_planner_schema() -> None: ...
def test_alignment_judged_with_answer_and_goal_text() -> None: ...
def test_unrelated_answer_fails_alignment() -> None:      # 用固定 judge 替身返回 needs_revision
```

**验收**：

```bash
.venv/bin/python -m pytest tests/test_rubric_judge.py tests/test_goal_alignment.py \
  tests/test_completion_readiness.py tests/test_sdk_runtime.py -q
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

**回滚**：撤销装配注册与 `GOAL_ALIGNMENT` 枚举；`judge` 签名可保留（默认参数化）以免连锁。

---

### Phase 4 — INV-12：续跑必须显式指名

**改动文件**：`src/tsm_agt/core/kernel.py`（路由规范化，`1820-2010`）、`tests/test_session_input_resolution.py`

在 `kernel.py:1927-1940` 的 `INDEPENDENT` 检查之后、`candidate_task_ids` 校验之前插入：

```python
# INV-12: a context-dependent input with resumable candidates must never
# silently become a brand-new Task with the raw utterance as its goal.
if (
    disposition is SessionRouteDisposition.CREATE_TASK
    and relation in {
        SessionTaskRelation.INDEPENDENT, SessionTaskRelation.CONTEXTUAL,
    }
    and input_grounding is not SessionInputGrounding.SELF_CONTAINED
    and candidates
):
    unique_candidates = tuple(dict.fromkeys(
        item.task_id for item in candidates
    ))
    if len(unique_candidates) == 1:
        source_task_id = unique_candidates[0]
        disposition = SessionRouteDisposition.CREATE_TASK
        relation = SessionTaskRelation.FOLLOW_UP
        handoff = build_session_follow_up_handoff(
            normalized, catalog_by_id[source_task_id]
        )
        resolved_goal = handoff.goal
        reason = "unique_candidate_context_derived_follow_up"
        clarification = None
    else:
        disposition = SessionRouteDisposition.CLARIFY
        relation = SessionTaskRelation.UNCERTAIN
        source_task_id = None
        resolved_goal = None
        reason = "multiple_candidates_require_explicit_choice"
        clarification = (
            "这条输入需要结合历史才能理解，且关联多项历史工作。"
            "请指定要接续的 Task，或完整描述一个新目标。"
        )
        candidate_task_ids = unique_candidates[:5]
```

并在紧随其后的 `CLARIFY` 分支（`kernel.py:1949-1965`）保持"≥1 个候选即可澄清"：

```python
if disposition is SessionRouteDisposition.CLARIFY:
    if not candidate_task_ids:                 # 原为 len(...) < 2
        ...degrade to CONTEXTUAL as today...
    else:
        clarification = clarification or "这条输入可能关联历史工作，请选择具体一项。"
```

**不改变**：`RESUME_TASK` 已显式携带 `source_task_id` 且要求 `confidence ≥ 0.85`，符合 INV-12，保持原样。
**不改变**：路由失败（`semantic_router_*_contextual_fallback`）在无候选时仍建 `CONTEXTUAL`（零候选分支）。

`candidate_task_ids` 当前在插入点之后才解析（`1941-1948`），因此实现时需把该解析上移到本段之前，
或在本段内直接用 `candidates` 并让后续解析复用；**保持 `candidate_task_ids` 的"必须存在于 catalog"校验不变**。

#### 测试

`tests/test_session_input_resolution.py` 追加：

```python
def test_context_dependent_with_single_candidate_resumes_it() -> None: ...
def test_context_dependent_with_multiple_candidates_clarifies() -> None: ...
def test_context_dependent_without_candidates_stays_contextual() -> None: ...
def test_router_failure_fallback_is_unchanged() -> None: ...
def test_self_contained_new_task_is_unaffected() -> None: ...
```

**验收**：

```bash
.venv/bin/python -m pytest tests/test_session_input_resolution.py tests/test_runtime_input_router.py -q
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

**回滚**：删除插入段并恢复 `len(candidate_task_ids) < 2`。

---

### Phase 5 — 压缩按 provenance 保留 pinned + 回执溯源

**改动文件**：`src/tsm_agt/core/context.py`、`tests/test_context_compaction.py`

#### 5.1 `ContextWindowManager` 新字段（`context.py:112-120`）

```python
pinned_message_ceiling: int = 32      # 新：pinned + recent 合并后的封顶
```

`__post_init__` 的正数校验加入该字段；`snapshot_data()` 一并输出。

#### 5.2 `_compact_session_messages`（`context.py:361-432`）

```python
pinned = {
    str(item) for item in body.get("pinned_task_ids", []) if item
}
recent = raw_messages[-self.recent_message_floor:]
if pinned:
    pinned_messages = [
        item for item in raw_messages
        if isinstance(item, dict) and str(item.get("task_id")) in pinned
    ]
    recent = pinned_messages + [
        item for item in recent if item not in pinned_messages
    ]
    recent = recent[-self.pinned_message_ceiling:]           # 与 P1 的 scoped 封顶同量级
kept_summaries = [
    item for item in summaries
    if isinstance(item, dict)
    and (
        str(item.get("task_id")) in recent_task_ids
        or str(item.get("task_id")) in pinned
    )
]
```

`historical_investigation.resources/questions` 的保留条件同样加入 `or source_task_id in pinned`。

#### 5.3 回执溯源（`context.py:68-101`、`434-465`）

`_compaction_receipt` 计算：

```python
preserved_ids = {m.message_id for m in preserved}
forgotten = tuple(
    m.message_id for m in source if m.message_id not in preserved_ids
)
forgotten_sequences = tuple(sorted({
    m.source_event_sequence for m in source
    if m.message_id not in preserved_ids and m.source_event_sequence > 0
}))
```

> `Message` 无 `source_event_sequence` 字段时，从 `session-context-…` 正文的
> `source_event_sequences` / `recent_messages[*].source_event_sequence` 提取；提取不到则为空元组。

#### 测试

`tests/test_context_compaction.py` 追加：

```python
def test_pinned_task_summary_survives_compaction() -> None: ...
def test_pinned_task_messages_survive_compaction() -> None: ...
def test_compaction_receipt_lists_forgotten_event_sequences() -> None: ...
def test_unpinned_old_summary_is_still_dropped() -> None: ...
```

**验收**：

```bash
.venv/bin/python -m pytest tests/test_context_compaction.py tests/test_model_streaming.py -q
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

**回滚**：删除 pinned 分支与三个回执字段（保持 `event_data()` 向后兼容）。

---

## 5. 不变量 → 验收矩阵

| INV | 断言 | 测试 | 阶段 |
|---|---|---|---|
| INV-8 | 任意会话内，Planner 上下文的 `scoped_messages` 无跨任务消息 | `test_planner_context_has_zero_unrelated_messages`、`test_property_selection_is_provenance_only` | P1 |
| INV-8 | pinned 摘要不因压缩丢失 | `test_pinned_task_summary_survives_compaction` | P5 |
| INV-9 | follow-up goal == 用户当前请求 | `test_follow_up_goal_contains_only_current_request` | P2 |
| INV-9 | 背景逐字片段不得成为判据 | `test_criterion_verbatim_from_background_is_demoted` | P2 |
| INV-10 | 无 judge 的判定判据 ⇒ 非 PASSED | `test_judged_criterion_without_judge_is_blocked_not_skipped` | P3 |
| INV-10 | `BLOCKED` ⇒ `NEEDS_REVIEW` 终态 | `test_blocked_verification_maps_to_needs_review` | P3 |
| INV-11 | ANSWER 任务必有 `goal_alignment` | `test_answer_task_gets_goal_alignment_criterion` | P3 |
| INV-11 | 答非所问被判定失败 | `test_unrelated_answer_fails_alignment` | P3 |
| INV-12 | 多候选 ⇒ `CLARIFY`；唯一候选 ⇒ 定向续跑 | `test_context_dependent_with_multiple_candidates_clarifies`、`test_context_dependent_with_single_candidate_resumes_it` | P4 |
| INV-12 | 零候选仍可建 CONTEXTUAL | `test_context_dependent_without_candidates_stays_contextual` | P4 |

---

## 6. 兼容性与迁移

| 变更 | 兼容策略 |
|---|---|
| `for_prompt(scope=...)` | 默认 `None` ⇒ 现状行为；所有既有调用点无需改动 |
| `body_data["recent_messages"]` | 保留为兼容别名（UI/旧测试可继续读） |
| `build_session_follow_up_goal` | 保留同名 facade，返回纯 goal；断言复合字符串的测试需更新 |
| `RubricJudgePort.judge` 签名 | 契约版本升 2.0；仅 `ModelRubricJudge` 一个实现；判据事件增加 `evidence_count` |
| 历史快照 | 全部新字段都有默认值，旧 `TaskSpec` / 旧 checkpoint 可回放（`from_data` 不硬失败） |
| `TaskCriterionKind.GOAL_ALIGNMENT` | 仅 Runtime 追加；从 Planner schema 排除，避免模型自撰 |

---

## 7. 回归与验收命令

```bash
# 定向
.venv/bin/python -m pytest \
  tests/test_context_scope_selection.py tests/test_authority_layering.py \
  tests/test_goal_alignment.py tests/test_rubric_judge.py \
  tests/test_session_context.py tests/test_session_handoff.py \
  tests/test_session_input_resolution.py tests/test_context_compaction.py \
  tests/test_completion_readiness.py tests/test_sdk_runtime.py -q

# 全量（基线对照）
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

**通过定义**：定向测试全绿；全量结果与 §8 基线**同为 8 个既有失败**，且失败集合不变。

---

## 8. 基线与已知失败（不得新增）

收尾后口径（2026-09，同一 venv；失败项已用 HEAD `a2a9c1e` 干净 worktree 逐项复现）：

```
1198 passed / 8 failed / 8 skipped / 175 subtests passed
失败（既有，与本次改动无关）：
  tests/test_cli_line_editing.py             ×5   OSError: out of pty devices
  tests/test_local_workspace_sandbox.py      ×2   OS 隔离 / 信任缓存环境
  tests/test_investigation_status.py         ×1   status.tool_calls 实测 0、期望 1
```

> 早期记录里的 `test_composition` ×2 与 `test_architecture/test_dependencies.py` ×1 **已修复**，
> 不再计入基线：前者是期望工具表过期（缺 `core.grep_search`）叠加一处测试不密闭，
> 后者是 Anthropic 适配器跨 provider 依赖（已抽 `adapters/http_json` 中立传输）。
> 详见《完成判定反面漏洞修复SPEC.md》§8。

**门禁**：若某阶段使既有失败集合扩大，或使 `test_composition` 数量变化，视为回归，必须修复后再进入下一阶段。

---

## 9. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| Planner 只收本任务消息后，续跑场景上下文不足 | 规划质量下降 | `related_task` 仍提供源任务身份/状态；`goal_alignment` 兜住跑偏 |
| `find_background_leak` 误报（合法复用短语） | 判据被误降级 | `min_span=24` + casefold；只降级不失败；事件可审计 |
| 装配 judge 增加模型调用成本 | 成本上升 | `rubric_judge_max_attempts=2`、证据 ≤4×800 字、`max_output_tokens=256` |
| `BLOCKED ⇒ NEEDS_REVIEW` 改变既有终态分布 | 更多任务进入人工复核 | 这正是 INV-10 的目的；`needs_review` 已有恢复入口 `resolve_needs_review` |
| `_compact_session_messages` 保留 pinned 导致 payload 变大 | 触发更频繁压缩 | `scoped_visible_message_limit=32` 与 `historical_resource_limit=24` 双重封顶 |
| 多候选 `CLARIFY` 改变路由行为 | 部分输入从"直接建任务"变为"提问" | 仅限"上下文依赖 + ≥2 候选"；零候选与 `SELF_CONTAINED` 不变 |

**整体回滚**：各阶段独立；按逆序回退（P5 → P1 → P3 → P4 → P2 无强制顺序）。

---

## 10. 实施状态表

| 阶段 | 内容 | 不变量 | 状态 |
|---|---|---|---|
| P0 | 基线固化 + 观测字段 | — | ✅ 完成 |
| P1 | provenance 选择（`ContextScope`） | INV-8 | ✅ 完成 |
| P2 | goal 纯化 + 权威分层 + 泄漏护栏 | INV-9 | ✅ 完成 |
| P3 | 判官装配 + 证据正文 + `goal_alignment` | INV-10/11 | ✅ 完成 |
| P4 | 唯一候选定向续跑 / 多候选澄清 | INV-12 | ✅ 完成 |
| P5 | 压缩保留 pinned + 回执溯源 | INV-8 | ✅ 完成 |

---

## 11. 实施记录与偏差（实际落地 vs 本文档计划）

实施完全遵循本 SPEC 的契约与不变量；以下为落地时相对计划的**实现细节偏差**，均为等价或更简方案：

| # | SPEC 原计划 | 实际实现 | 原因 |
|---|---|---|---|
| 1 | `for_prompt` 的 scoped 与 background 并列输出 | legacy 路径（`scope=None`）**只输出 `recent_messages`**，`scoped_messages`/`background_messages` 为空；scoped 路径才输出两个新字段，`recent_messages` 为空 | 避免同一内容在 payload 中出现两次而翻倍 prompt 预算（曾导致 CLI 第 4 轮 `ContextWindowExceeded`） |
| 2 | `context_handoff` 只加到 `submit_task` | 加到 `Kernel.create_task` 与 SDK `submit_task`/`create_task`；SDK 在 CREATE_TASK 主路径从 `decision.task_catalog` 确定性重建 background | 主路径不经 `submit_task` 的防御分支；复用同一 `build_session_follow_up_handoff`，无逻辑分叉 |
| 3 | Planner 的 `source_task_id` 解析沿用现有回退 | 改为**优先读本任务 `task.created` payload**（权威来源），并新增 `get_task` 兜底生成最小 `related_task` | 原回退依赖 recency 窗口内的摘要，正是 INV-8 要消除的耦合；无摘要的 interrupted 源任务也需要身份 |
| 4 | 每次判定判据无 judge ⇒ 直接 `_finalize_needs_review` | 在 `_completion_readiness_gaps` 增加 `JUDGED_CRITERION_WITHOUT_JUDGE`（required、无 effects），并加入 `_JUDGED_GAP_KINDS` 免于 INV-3 降级 | readiness 评估返回决策给可替换策略，直接终结会绕过策略；新增 gap 经 stall cap 有界进入 `NEEDS_REVIEW`，同样满足 INV-4/INV-10 |
| 5 | `forgotten_event_sequences` 取"被丢弃消息"的序列 | 同时取"就地裁剪"的差集（压缩前后 session 投影序列之差） | `session-context-…` 消息本体受保护不会整条丢弃，只被裁剪，仅看丢弃消息会永远为空 |
| 6 | 更新断言复合 goal 的测试 | 迁移了 `test_session_handoff`、`test_derived_task_goal_authorship`、`test_session_text_facade`、`test_session_input_resolution`、`test_task_spec_criteria_validation`、`test_cli_chat`、`test_production_journeys`、`test_rubric_judge` | 这些测试固定的是旧契约（goal 含源任务段 / rubric 无 judge 可空洞通过 / judge 只收引用） |

**新增文件**：`tests/test_context_scope_selection.py`、`tests/test_authority_layering.py`、`tests/test_goal_alignment.py`。

**改动文件（主要）**：
`src/tsm_agt/core/session_context.py`（`ContextAuthority`/`ContextScope`/`select_messages_by_scope`/`for_prompt`）、
`src/tsm_agt/core/session_handoff.py`（`SessionFollowUpHandoff`/`clip_text`/`find_background_leak`）、
`src/tsm_agt/core/kernel.py`（Planner scope、`_demote_background_leaks`、`_rubric_evidence`、`GOAL_ALIGNMENT`、INV-12 路由、`JUDGED_CRITERION_WITHOUT_JUDGE`）、
`src/tsm_agt/core/task_spec.py`（`GOAL_ALIGNMENT` + schema 排除）、
`src/tsm_agt/core/context.py`（`pinned_message_ceiling`、pinned 保留、回执溯源）、
`src/tsm_agt/ports/rubric_judge.py`（`RubricEvidence` + 端口 v2）、
`src/tsm_agt/adapters/model_rubric_judge/judge.py`（证据正文）、
`src/tsm_agt/bootstrap/composition.py`（装配 `ModelRubricJudge`）、
`src/tsm_agt/sdk/runtime.py`（`context_handoff`、`BLOCKED ⇒ NEEDS_REVIEW`）、
`src/tsm_agt/adapters/model_task_spec_planner/planner.py`（按档位渲染）。
