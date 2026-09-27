# 验收判定改造实施 SPEC（可执行版）

状态：待实施
设计基线：《验收判定通用流程方案.md》（原则层，本文件是它的可执行落地）
替代关系：本文件吸收 `完成判定改造SPEC.md` 的 §10.0 第 0 条，并修复其未覆盖的同类闭环
关联事故：`task-2c8f8fbb75714516a94ab442995626b5`
实施原则：**只加不删、默认值兼容、历史事件可回放、每个 Phase 可独立发布与回退**

---

## 0. 术语速查

| 英文 | 中文 | 本文用法 |
|---|---|---|
| criterion | 判据、验收条款 | `TaskSpec.acceptance_criteria` 的一条 |
| verification kind | 验证类型 | `workspace_integrity` / `post_mutation_command` / `evidence_reference` / 新增 `rubric` |
| evidence_reference | 证据引用 | 形如 `event:N` / `tool_call:ID` / `mutation:ID` 的字符串 |
| gap | 缺口 | 一条"未达标"的记录 |
| required | 必需的 | 为真时参与阻塞 |
| advisory | 提示性的 | 只记录，永不阻塞 |
| closable | 可闭合的 | 有办法让它从不达标变成达标 |
| demote | 降级 | 把 required 判据改成 advisory |
| terminal state | 终态 | 不再自动变化的状态 |
| NEEDS_REVIEW | 待人工复核 | 本次新增的终态 |
| fail-closed | 失败即关闭 | 判定器出错时默认"不通过" |
| replay | 回放 | 用历史事件重建任务状态 |

---

## 1. 目标与非目标

### 1.1 目标

1. 规划期就拒绝/降级"无法验证"的判据，杜绝非法 `evidence_reference` 落库。
2. 任何情况下都不再产生**不可闭合的必需 gap**（消灭无限 `AWAITING_USER`）。
3. 完成判定器崩溃时 **fail-closed**（当前是 fail-open，见 P0-3）。
4. 给"到顶 / 验不了"提供一个明确终态 `NEEDS_REVIEW`。
5. 为"判断型判据"提供非阻塞通道（`rubric`）。

### 1.2 非目标（本 SPEC 不做）

- 不重构 `TaskState` 为完整的五轴正交模型（本 SPEC 只新增一个终态；五轴在 §11 登记）。
- 不删除 `evidence_reference` 这一类型。
- 不引入新的持久化实体、不新增账本。
- 不改变 `_verify_task_spec_reference` 已支持的 `event:` / `tool_call:` / `mutation:` 语义。

---

## 2. 现状锚点（已核实）

### 2.1 当前流程

两道门：

1. **轮内门**：`Kernel._evaluate_completion_readiness`（`kernel.py:7532`）→ `_completion_readiness_gaps`（`kernel.py:7224`）→ `RuleBasedCompletionReadinessPolicy.evaluate`（`adapters/rule_based_completion_readiness/policy.py:56`）。
2. **任务门**：SDK `_finish_agent_result`（`sdk/runtime.py:1187`）→ `Kernel.verify_task_acceptance`（`kernel.py:8429`）→ `transition_task` 守卫（`kernel.py:8309`）。

### 2.2 缺陷锚点

| 缺陷 | 位置 | 证据 |
|---|---|---|
| D1 `evidence_reference` 不校验格式 | `task_spec.py:701-710`（只查非空） | 事故任务存有 `turn-922dbad9...` |
| D2 `TASK_SPEC_EVIDENCE` 不可闭合却 `required=True` | `kernel.py:7393-7400` | `required_effects=()`、`recoverable=False` |
| D3 不可闭合 → 永不 `CONTINUE` | `policy.py:71-75, 96-102` | `effective_required_effects` 为空 |
| D4 直接挂起为 `AWAITING_USER` | `kernel.py:12547-12574`, `12945-13037` | 事件 seq 24–26 |
| D5 判定器崩溃默认通过 | `kernel.py:7610-7620` | `except ... → COMPLETE, "policy_failed"` |
| D6 `AWAITING_USER` 无终态出口 | `task.py:140-142` | 后继只有 EXECUTING/RUNNING_WORKFLOW/INTERRUPTING/CANCELLED |

### 2.3 事故时间线（作为回归用例）

```
seq 18  task_spec.revised (r2, writer=task-spec-planner)
        3 条 evidence_reference 指向 turn-...
seq 19  model.attempt_started
seq 20  model.attempt_completed   ← 模型已给出完整回答
seq 21  completion.readiness_evaluated  action=REPORT_BLOCKED gap_count=3
seq 24  continuation.requested    reason=unmet_acceptance_criteria
seq 26  task.state_changed        EXECUTING → AWAITING_USER
之后    用户输入被 sdk/runtime.py:526-537 拒绝
```

---

## 3. 设计规则

### 3.1 七条不变量（必须由测试锁定）

```
INV-1  计划层（WorkingMemory plan / todo / step 状态）永不参与成功判定。
INV-2  任何判据在写入合同之前，必须通过"可验证性"校验；不过则降级或返工。
INV-3  不允许存在"必需且不可闭合"的 gap。required=True ⇒ 存在闭合途径，或降级为 advisory。
INV-4  每个循环有上限；到顶必须升级为 NEEDS_REVIEW，never loop forever。
INV-5  判定器异常一律 fail-closed，并 fail-loud（写事件 + 进入可解释状态）。
INV-6  完成结论至少三值：PASSED / FAILED / 人工复核（NEEDS_REVIEW）。
INV-7  验证器只依据 ledger / 执行事实，不依据模型自述（措辞启发式除外，且只允许保守方向）。
```

### 3.2 核心判据语义（本 SPEC 的立论）

> **`evidence_reference` 是"去确认一条已经成立的断言"，不是"要去做的工作"。**
>
> - 它**不能被当作"待办"**：引用不存在时，无法通过执行去"造出"一个指定 id。
> - 需要"去做"的，必须建模为带 `required_effects` 的 Outcome（例如 `EVIDENCE` + `OBSERVE`）。
>
> 推论：**任何未被满足的 `evidence_reference` 判据，在轮内门里只能是 advisory，不能是 required。**
> 它仍然会在任务门（`verify_task_acceptance`）里被评估，不通过则任务 `FAILED`（可见、可解释），而不是挂起。

---

## 4. 改动总览

| ID | 改动 | 主要文件 | 规模 | 解决 | 依赖 |
|---|---|---|---|---|---|
| **P0-1** | 判据作者期校验 + 不可解析判据降级为 `rubric` | `task_spec.py`, `kernel.py:2889/2930`, `planner.py:62-67` | ~70 行 | D1 | 无 |
| **P0-2** | `TASK_SPEC_EVIDENCE` 缺口改为 advisory + 不变量防守 | `kernel.py:7393-7400` | ~25 行 | D2/D3 | 无 |
| **P0-3** | 完成判定器异常改 fail-closed | `kernel.py:7602-7620`, `7644-7669` | ~15 行 | D5 | 无 |
| **P0-4** | 补 CONTINUATION 的结构化 resume 通道 | `sdk/runtime.py`, `web/app.py` | ~50 行 | D4 的用户面 | 无 |
| **P1-1** | 新增终态 `NEEDS_REVIEW` | `task.py`, `sdk/runtime.py`, `web/app.py` | ~60 行 | D6 | 无 |
| **P1-2** | 停滞到顶不再挂起，改终态 | `kernel.py:12945-13037`, `9945-10130` | ~70 行 | D4/D6 | P1-1 |
| **P1-3** | 终态权限矩阵 | `kernel.py:8280-8390` | ~40 行 | — | P1-1 |
| **P2-1** | `rubric` 判据 + 有界评审员 | 新 `ports/rubric_judge.py` + adapter + `kernel.py:8429` | ~180 行 | D1 的余量 | P0-1 |
| **P3-1** | `NEEDS_REVIEW` 的人工三选项 | `kernel.py`, `web/app.py` | ~120 行 | D6 的收尾 | P1-1 |

发布策略：**P0 一批发布；P1 一批；P2、P3 各自独立。** 每批都能单独回退（见 §9.3）。

---

## 5. P0：止血与不变量

### P0-1 判据作者期校验 + 降级

#### 目标
非法 `evidence_reference` 不再落库；无法解析的判据自动降级为 `rubric`（advisory）。

#### 关键约束（**必须遵守，否则破坏回放**）
校验**不得**放在 `TaskAcceptanceCriterion.__post_init__` 或 `from_data` 里。
原因：事故任务 `task-2c8f8fbb...` 的历史 `task_spec.revised` 事件里就存着 `turn-...`，
在 `__post_init__` 抛异常会让该任务**无法回放**。

**校验只放在"作者期（authoring time）"**，即新写合同的两条路径：
1. `Kernel.plan_task_spec`（`kernel.py:2889`，Planner 路径）
2. `Kernel.revise_task_spec`（`kernel.py:2930`，模型经 `core.task_spec_update` 路径）

#### 改动 1：`task_spec.py` 新增 `rubric` 类型与校验辅助

```python
class TaskCriterionKind(StrEnum):
    WORKSPACE_INTEGRITY = "workspace_integrity"
    POST_MUTATION_COMMAND = "post_mutation_command"
    EVIDENCE_REFERENCE = "evidence_reference"
    RUBRIC = "rubric"                    # 新增：判断型通道，默认不阻塞


#: 只有这三种前缀能被 `_verify_task_spec_reference` 解析。
MACHINE_REFERENCE_PREFIXES: tuple[str, ...] = ("event:", "tool_call:", "mutation:")


def validate_authored_reference(kind: TaskCriterionKind, reference: str | None) -> None:
    """作者期校验。只允许在 plan / revise 路径调用，禁止进入 __post_init__。"""
    if kind is not TaskCriterionKind.EVIDENCE_REFERENCE:
        return
    ref = (reference or "").strip()
    if not ref:
        raise ValueError("evidence_reference criterion requires a reference")
    if not ref.startswith(MACHINE_REFERENCE_PREFIXES):
        raise ValueError(
            "evidence_reference must start with one of "
            + "/".join(MACHINE_REFERENCE_PREFIXES)
            + f"; got {ref!r}"
        )
```

`TaskAcceptanceCriterion.from_data` **保持现状不动**（放宽读取，保证历史兼容）。
`TaskAcceptanceCriterion.to_data` 不改（无新字段，零 schema 变更）。

#### 改动 2：`kernel.py` 新增降级器

```python
from tsm_agt.core.task_spec import (
    TaskCriterionKind, validate_authored_reference,
)
# 说明：TaskAcceptanceCriterion / TaskCriterionKind 已在 kernel.py 既有的
# `from .task_spec import (...)` 中导入；这里只需追加 validate_authored_reference。

def _demote_unresolvable_criteria(
    self, task: TaskSnapshot, events: tuple[RuntimeEvent, ...],
    criteria: tuple[TaskAcceptanceCriterion, ...],
) -> tuple[tuple[TaskAcceptanceCriterion, ...], list[dict[str, str]]]:
    """把无法解析的 evidence_reference 判据降级为 rubric（advisory）。

    依据 §3.2：evidence_reference 是"确认既有断言"，不是"待办"。
    无法解析 ⇒ 永远无法通过 ⇒ 不允许作为 required 判据存在。
    """
    kept: list[TaskAcceptanceCriterion] = []
    demoted: list[dict[str, str]] = []
    for criterion in criteria:
        if criterion.verification_kind is not TaskCriterionKind.EVIDENCE_REFERENCE:
            kept.append(criterion)
            continue
        reference = criterion.evidence_reference or ""
        # 格式在作者期已校验；这里校验"当前是否可解析"。
        evidence = self._verify_task_spec_reference(
            task, events, reference, criterion.description
        )
        if evidence.passed:
            kept.append(criterion)
            continue
        kept.append(TaskAcceptanceCriterion(
            criterion_id=criterion.criterion_id,
            description=criterion.description,
            verification_kind=TaskCriterionKind.RUBRIC,
            evidence_reference=None,
        ))
        demoted.append({
            "criterion_id": criterion.criterion_id,
            "from": TaskCriterionKind.EVIDENCE_REFERENCE.value,
            "to": TaskCriterionKind.RUBRIC.value,
            "reference": reference,
            "reason": evidence.observed,
        })
    return tuple(kept), demoted
```

#### 改动 3：接入 `plan_task_spec`

在 `kernel.py:2907-2912` 之间插入：

```python
proposal = TaskSpecProposal.from_data(raw, require_acceptance_criteria=True)
# 作者期格式校验：非法直接触发 Planner 的 bounded correction。
for criterion in proposal.acceptance_criteria:
    validate_authored_reference(
        criterion.verification_kind, criterion.evidence_reference
    )
# 可解析性校验：不可解析的降级为 rubric，并留痕。
events = await self._dependencies.store.read_events(task_id)
criteria, demoted = self._demote_unresolvable_criteria(
    task, events, proposal.acceptance_criteria
)
if demoted:
    proposal = replace(proposal, acceptance_criteria=criteria)
    await self._append_events(task_id, (("task_spec.criteria_demoted", {
        "writer": "task-spec-planner", "demoted": demoted,
    }),))
candidate = TaskSpecSnapshot.from_proposal(task_id, current.revision + 1, proposal)
```

> `validate_authored_reference` 抛 `ValueError` 时，会走到 `kernel.py:2923` 的
> `task_spec.planning_failed` 并 re-raise；Planner 侧已在
> `adapters/model_task_spec_planner/planner.py:143-171` 做了 2 次 bounded correction，
> 因此正常情况第二次就会改成合法格式。

#### 改动 4：接入 `revise_task_spec`

在 `kernel.py:2936`（方法体开头）后加：

```python
for item in acceptance_criteria:
    validate_authored_reference(item.verification_kind, item.evidence_reference)
```

这样 `core.task_spec_update`（模型在**执行期**把判据绑定到真实的 `event:`/`tool_call:`）依然可用，
但非法格式会在工具层被拒（`task_spec_tools.py:113-114` 已把 `ValueError` 映射为 `INVALID_PARAM`）。

#### 改动 5：Planner 提示

`adapters/model_task_spec_planner/planner.py:62-67`，把这段：

```
"use workspace_integrity for durable workspace changes, "
"post_mutation_command when a mutation must be command-verified, "
"and evidence_reference only with a durable Runtime reference. "
```

改为：

```
"use workspace_integrity for durable workspace changes, "
"post_mutation_command when a mutation must be command-verified, "
"and rubric for any judgement that cannot be machine-checked. "
"Do NOT invent evidence_reference at planning time: it may only name an "
"event:/tool_call:/mutation: reference that already exists in this Task. "
"If a requirement needs work to be done, express it as an Outcome with "
"required_effects instead of an evidence_reference criterion. "
```

#### 事件
- `task_spec.criteria_demoted`（新增）
  payload: `{writer, demoted: [{criterion_id, from, to, reference, reason}]}`

#### 测试
文件：`tests/test_task_spec_criteria_validation.py`（新增）

| 用例 | 断言 |
|---|---|
| `turn-...` 引用 | `validate_authored_reference` 抛 `ValueError` |
| `event:1` 引用 | 不抛 |
| 历史快照含 `turn-...` | `TaskAcceptanceCriterion.from_data` **不抛**（回放安全） |
| `plan_task_spec` 遇到不可解析引用 | 判据被降级为 `rubric`，`task_spec.criteria_demoted` 事件存在，任务继续 |
| `revise_task_spec` 传 `turn-...` | 抛 `ValueError`；`core.task_spec_update` 返回 `INVALID_PARAM` |

#### 验收口径
- 事故任务的同类输入不再产生 `required=True` 的 `task-spec:*` gap。
- `pytest tests/test_task_spec.py tests/test_task_spec_criteria_validation.py` 全绿。

#### 回退
删除 `_demote_unresolvable_criteria` 的调用与 `validate_authored_reference` 调用即可；
`rubric` 枚举值保留不影响旧数据。

---

### P0-2 `TASK_SPEC_EVIDENCE` 缺口改 advisory + 不变量防守

#### 目标
消灭"必需且不可闭合"的 gap，直接堵死事故闭环。

#### 改动 1：`kernel.py:7388-7400`

现状：

```python
else:
    reference = criterion.evidence_reference or ""
    evidence = self._verify_task_spec_reference(
        task, events, reference, criterion.description
    )
    if not evidence.passed:
        gaps.append(CompletionGap(
            gap_id=gap_id,
            kind="TASK_SPEC_EVIDENCE",
            description=criterion.description,
            status="MISSING", required=True, recoverable=False,
            evidence_reference=reference,
        ))
```

改为：

```python
else:
    reference = criterion.evidence_reference or ""
    evidence = self._verify_task_spec_reference(
        task, events, reference, criterion.description
    )
    if not evidence.passed:
        # §3.2: an evidence_reference criterion asserts an already-true fact.
        # It is never work to be done, so an unmet one must not block the Turn
        # as a required gap. It is still evaluated at Final Acceptance, where
        # failure is visible (task FAILED) instead of hanging as AWAITING_USER.
        gaps.append(CompletionGap(
            gap_id=gap_id,
            kind="TASK_SPEC_EVIDENCE",
            description=criterion.description,
            status="MISSING",
            required=False,            # ← 由 True 改为 False
            recoverable=False,
            evidence_reference=reference,
        ))
```

#### 改动 2：不变量防守（防御性，覆盖未来新 gap kind）

在 `_completion_readiness_gaps` 结尾的 `return tuple(gaps)`（`kernel.py:7502`）之前插入：

```python
# INV-3: a required gap must be closable. A required gap with no effective
# effects and no judged kind can never be closed, which is exactly the
# dead-lock shape of task-2c8f8fbb75714516a94ab442995626b5. Demote it and
# record the violation instead of suspending the Task forever.
_JUDGED_GAP_KINDS = frozenset({"TASK_SPEC_RUBRIC", "EVIDENCE_QUESTION"})
_violations = [
    gap for gap in gaps
    if gap.required
    and not gap.effective_required_effects
    and gap.kind not in _JUDGED_GAP_KINDS
]
if _violations:
    _violation_ids = {gap.gap_id for gap in _violations}
    gaps = [
        replace(gap, required=False) if gap.gap_id in _violation_ids else gap
        for gap in gaps
    ]
    await self._append_events(task_id, (("completion.invariant_violated", {
        "violations": [
            {"gap_id": gap.gap_id, "kind": gap.kind} for gap in _violations
        ],
    }),))
return tuple(gaps)
```

#### 为什么不改函数返回值（重要）

`_completion_readiness_gaps` 有 **3 个内部调用点**（`kernel.py:7541`、`11754`、`12067`）
和 **6 个测试调用点**（`tests/test_completion_readiness.py` ×5、`tests/test_post_mutation_applicability.py` ×1）。
因此：

- **返回值类型保持 `tuple[CompletionGap, ...]` 不变**；
- 降级在函数内**原地完成**，违规信息用新事件 `completion.invariant_violated` 留痕；
- 不在 `_evaluate_completion_readiness` 的 payload 里新增字段。

> 说明：`CompletionGap` 是 `@dataclass(frozen=True, slots=True)`
> （`ports/completion_readiness.py:29`），`dataclasses.replace` 可用；
> `gaps` 在函数内是 `list`，末尾统一 `return tuple(gaps)`。
> 降级后的 gap 会让现有 6 个测试看到 `required=False`——这是**预期的新语义**，
> 需同步检查这些测试中"依赖 required 为真"的断言（见 §10.2）。

#### 测试
文件：`tests/test_completion_readiness.py`（追加）

| 用例 | 断言 |
|---|---|
| 判据为 `evidence_reference` 且引用不可解析 | 产出 gap 的 `required is False` |
| 人为构造 `required=True` 且 `required_effects=()` 的 gap | 被降级，且事件含 `invariant_violations` |
| 事故回归：复刻 `turn-...` 三条判据 | **不产生**导致 `AWAITING_USER` 的必需 gap |

#### 验收口径
- 全库搜索：不存在 kind 为 `TASK_SPEC_EVIDENCE` 且 `required=True` 的 gap（测试断言）。
- 事故形状的任务以 `COMPLETE` 或 `FAILED` 结束，绝不长期停留 `AWAITING_USER`。

#### 回退
把 `required=False` 改回 `True`，移除不变量防守块。

---

### P0-3 完成判定器异常改 fail-closed

#### 目标
判定器崩溃时不再默认"通过"。

#### 改动 1：`kernel.py:7602-7620`

现状：

```python
if policy is None:
    decision = CompletionReadinessDecision(
        CompletionReadinessAction.COMPLETE, "policy_not_configured", state, gaps,
    )
else:
    try:
        decision = await policy.evaluate(probe, state)
    except Exception as error:
        await self._append_events(task_id, ((
            "completion.readiness_failed", {...},
        ),))
        decision = CompletionReadinessDecision(
            CompletionReadinessAction.COMPLETE, "policy_failed", state, gaps,
        )
```

改为：

```python
if policy is None:
    # Missing policy is a configuration fact, not a crash. Keep COMPLETE for
    # backward compatibility with compositions that intentionally omit it.
    decision = CompletionReadinessDecision(
        CompletionReadinessAction.COMPLETE, "policy_not_configured", state, gaps,
    )
else:
    try:
        decision = await policy.evaluate(probe, state)
    except Exception as error:
        await self._append_events(task_id, ((
            "completion.readiness_failed", {
                "turn_id": turn_id,
                "error_type": type(error).__name__,
                "fail_closed": True,
            },
        ),))
        # INV-5: never accept a final answer on a crashed judge.
        # Let the caller surface a defined failure instead of success.
        raise
```

#### 改动 2：诊断调用点必须不受影响

`_inject_agent_decides_completion_diagnostics`（`kernel.py:7644-7669`）会调用同一方法。
在 `kernel.py:7651` 外包一层：

```python
try:
    decision = await self._evaluate_completion_readiness(
        checkpoint.task_id, checkpoint.turn_id, checkpoint, visible_tools,
        forced_wrap_up=False,
    )
except Exception:
    # Diagnostics are observational and must never break a Turn.
    return checkpoint
```

主调用点 `kernel.py:12378` 不加捕获，让异常向上传播到
`sdk/runtime.py:721-734` 的兜底：任务被置为 `FAILED`（fail-closed、可见）。

#### 测试
文件：`tests/test_completion_readiness.py`（追加）

| 用例 | 断言 |
|---|---|
| 注入一个必抛异常的 policy | 事件 `completion.readiness_failed` 的 `fail_closed is True`；任务最终 `FAILED`；**不出现** `SUCCEEDED` |
| `AGENT_DECIDES` 模式下 policy 抛异常 | 诊断被跳过，Turn 正常返回（不崩） |

#### 回退
恢复 `except` 分支返回 `COMPLETE`。

---

### P0-4 补 CONTINUATION 的结构化 resume 通道

#### 目标
让合法的 CONTINUATION 有明确入口，而不是靠发普通文本碰运气。

#### 改动 1：SDK

在 `sdk/runtime.py` 增加（紧邻 `stop` / `cancel` 便利方法）：

```python
async def resume_continuation(
    self, task_id: str, *, command_id: str, text: str,
) -> RuntimeCommandResult:
    """Explicitly resume a Task suspended at a CONTINUATION boundary."""
    async def execute():
        return await self.run_task(task_id, text)
    return await self._command(
        command_id, "resume_continuation",
        {"task_id": task_id, "text_hash": canonical_hash(text)},
        execute,
        lambda result: result.to_data(),
    )
```

> `run_task`（`sdk/runtime.py:747-792`）已经处理
> `AWAITING_USER + pending_user_action.kind == "CONTINUATION"` → `resume_agent_continuation`。

#### 改动 2：Web 端点

在 `web/app.py` 增加，与 `/tasks/{id}/stop` 并列：

```python
@app.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, payload: dict | None = None) -> dict:
    if runtime_server is None:
        raise HTTPException(status_code=503, detail=runtime_error or "runtime unavailable")
    body = payload or {}
    command_id = str(body.get("command_id", "")).strip() or f"web-resume-{task_id}"
    text = str(body.get("text", "")).strip() or "继续"
    data = runtime_server._call(runtime_server._client.resume_continuation(
        task_id, command_id=command_id, text=text,
    ))
    return dict(data)
```

前端：当 `waiting.kind === "CONTINUATION"` 时渲染一个"继续"按钮，点击 POST 该端点。
（`_waiting_payload` 目前在 `web/app.py:3334-3347` 把 CONTINUATION 退化成 `kind="INPUT"`，
本改动同时把 `kind` 修正为 `"CONTINUATION"`。）

#### 测试
文件：`tests/test_sdk_runtime.py`（追加）、`tests/test_web_phase1_state.py`（追加）

| 用例 | 断言 |
|---|---|
| `resume_continuation` 对 CONTINUATION 任务 | 返回 `task`，任务回到 `EXECUTING`/`SUCCEEDED` |
| `resume_continuation` 对非 CONTINUATION 任务 | SDK 走 `get_task_result`，不误恢复 |
| `POST /tasks/{id}/resume` | 202 |

#### 回退
删除端点与 SDK 方法（不影响其它逻辑）。

---

## 6. P1：终态与有界

### P1-1 新增终态 `NEEDS_REVIEW`

#### 改动 1：`task.py`

```python
class Phase1TaskState(StrEnum):
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    INTERRUPTED = "INTERRUPTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"     # 新增
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskState(StrEnum):
    ...
    NEEDS_REVIEW = "NEEDS_REVIEW"     # 新增

    @property
    def is_terminal(self) -> bool:
        return self in {
            self.SUCCEEDED, self.CANCELLED, self.FAILED, self.NEEDS_REVIEW,
        }

    @property
    def phase1_state(self) -> Phase1TaskState:
        ...
        if self is self.NEEDS_REVIEW:
            return Phase1TaskState.NEEDS_REVIEW
        ...
```

`LEGAL_TRANSITIONS` 增加：

```python
TaskState.EXECUTING: frozenset({..., TaskState.NEEDS_REVIEW}),
TaskState.AWAITING_USER: frozenset({
    TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW,
    TaskState.INTERRUPTING, TaskState.CANCELLED,
    TaskState.NEEDS_REVIEW,           # ← 新增
}),
TaskState.VERIFYING: frozenset({
    TaskState.EXECUTING, TaskState.FINALIZING,
    TaskState.CANCELLED, TaskState.FAILED, TaskState.NEEDS_REVIEW,
}),
TaskState.NEEDS_REVIEW: frozenset(),  # P1 阶段为终态；P3 再开"重开"边
```

> 注意：`NEEDS_REVIEW` 为终态，因此 `transition_task` 里
> `FINALIZING/SUCCEEDED` 的验证守卫（`kernel.py:8309-8325`）不适用，
> 逻辑天然正确，无需改动。

#### 改动 2：SDK 状态映射

`sdk/runtime.py:1296-1305` 的 `_status_for_state`，把第 1303-1304 行：

```python
    if state.is_terminal:
        return "completed" if state is TaskState.SUCCEEDED else "failed"
```

改为：

```python
    if state.is_terminal:
        if state is TaskState.SUCCEEDED:
            return "completed"
        if state is TaskState.NEEDS_REVIEW:
            return "needs_review"       # 新增
        return "failed"
```

（不要重写整个函数：前面的 `AWAITING_APPROVAL` / `AWAITING_USER` / `INTERRUPTED`
分支必须保留。）

`sdk/runtime.py:456` 的 `_TRANSITIONAL_TASK_STATES` 不含 `NEEDS_REVIEW`，无需改。
`sdk/runtime.py:769-791` 的 `run_task` 分支对终态走 `get_task_result`，无需改。

#### 改动 3：Web 展示

- `web/app.py` 的 `_waiting_payload` 不需要改（NEEDS_REVIEW 不是 waiting）。
- `_task_status` / 任务卡片：`needs_review` 显示为"待人工复核（黄色）"，
  与 `completed`（绿）、`failed`（红）区分。
- `_describe_loop_limit` 文案（`web/app.py:3321`）追加一句：
  "若任务进入待人工复核，可在任务卡片上选择接受/退回/取消。"

#### 测试
文件：`tests/test_task_lifecycle_needs_review.py`（新增）

| 用例 | 断言 |
|---|---|
| `TaskState.NEEDS_REVIEW.is_terminal` | True |
| `phase1_state` | `Phase1TaskState.NEEDS_REVIEW` |
| `EXECUTING → NEEDS_REVIEW` | 合法 |
| `NEEDS_REVIEW → SUCCEEDED` | 非法（P1 阶段） |
| SDK `_status_for_state` | `"needs_review"` |

#### 回退
保留枚举值但不产生该状态；旧数据不含它，回放不受影响。

---

### P1-2 停滞到顶不再挂起，改终态

#### 目标
`stalled_continuations` 到顶后终结为 `NEEDS_REVIEW`，消灭无限循环。

#### 改动 1：可配置上限

`kernel.py` 的 `KernelDependencies`（结构体定义处）新增：

```python
completion_max_stalled_continuations: int = 2
```

composition 默认注册时透传（与 `RuleBasedCompletionReadinessPolicy` 的
`max_stalled_continuations` 保持一致，避免两处阈值漂移）。

#### 改动 2：`_suspend_incomplete_recoverable`（`kernel.py:12945-13037`）

在算出 `stalled` 之后、构造 `pending` 之前插入：

```python
if stalled >= self._dependencies.completion_max_stalled_continuations:
    return await self._finalize_needs_review(
        checkpoint, gaps,
        reason="required_work_unverifiable_or_stalled",
        stalled=stalled,
    )
```

#### 改动 3：新增 `_finalize_needs_review`

```python
async def _finalize_needs_review(
    self, checkpoint: AgentTurnCheckpoint, gaps: tuple[CompletionGap, ...],
    *, reason: str, stalled: int,
) -> AgentContinuationSuspended:
    """Terminate an unclosable required gap as a human-review state.

    This is the bounded escape hatch of INV-4: after the stall cap, Runtime
    must stop and hand the decision to a human instead of suspending forever.
    """
    stored = await self._require_stored_task(checkpoint.task_id)
    task = TaskSnapshot.from_data(stored.data)
    if task.state not in {TaskState.EXECUTING, TaskState.AWAITING_USER}:
        raise InvalidTurnState(
            f"needs-review finalization requires EXECUTING/AWAITING_USER, "
            f"got {task.state.value}"
        )
    finalized = task.transition(TaskState.NEEDS_REVIEW).with_agent_checkpoint(
        replace(
            checkpoint,
            revision=checkpoint.revision + 1,   # 与 _suspend_incomplete_recoverable 一致
            pending_user_action={},
        ).to_data()
    )
    events = (
        RuntimeEvent(
            f"evt-{uuid4().hex}", checkpoint.task_id,
            stored.last_event_sequence + 1, "completion.finalized_needs_review", {
                "turn_id": checkpoint.turn_id,
                "reason": reason,
                "stalled_continuations": stalled,
                "unmet_gap_ids": [gap.gap_id for gap in gaps],
            },
        ),
        RuntimeEvent(
            f"evt-{uuid4().hex}", checkpoint.task_id,
            stored.last_event_sequence + 2, "task.state_changed", {
                "previous_state": task.state.value,
                "next_state": finalized.state.value,
                "reason": reason,
            },
        ),
    )
    await self._dependencies.store.commit(RuntimeUnitOfWork(
        checkpoint.task_id, stored.version, finalized.to_data(), events
    ))
    return AgentContinuationSuspended(
        checkpoint.task_id, checkpoint.turn_id, checkpoint.revision,
        (), tuple(gap.gap_id for gap in gaps), checkpoint.messages[-1],
    )
```

> 返回类型沿用 `AgentContinuationSuspended`，SDK 侧通过任务状态区分终态；
> 若更希望语义清晰，可新增 `AgentNeedsReviewFinalized`，但会扩大改动面，
> 本 SPEC 选择复用（`sdk/runtime.py:1171-1186` 已能读取任务状态）。

#### 改动 4：`resume_agent_continuation` 入口防守

`kernel.py:9945-9967`，在读取 `pending` 后插入：

```python
readiness_state = CompletionReadinessState.from_data(
    checkpoint.completion_readiness_state
)
if (
    readiness_state.stalled_continuations
    >= self._dependencies.completion_max_stalled_continuations
):
    # Never re-enter a loop that is already known to be unproductive.
    awaited = await self._finalize_needs_review(
        checkpoint, (), reason="stalled_continuation_refused",
        stalled=readiness_state.stalled_continuations,
    )
    return awaited
```

#### 事件
- `completion.finalized_needs_review`（新增）
  payload: `{turn_id, reason, stalled_continuations, unmet_gap_ids}`

#### 测试
文件：`tests/test_completion_stall_terminal.py`（新增）

| 用例 | 断言 |
|---|---|
| 不可闭合必需 gap 连续 3 次 continue | 第 3 次后进入 `NEEDS_REVIEW`，不再 `AWAITING_USER` |
| 对 `NEEDS_REVIEW` 调 `resume_agent_continuation` | 抛 `InvalidTurnState`（非 AWAITING_USER） |
| 有进展的续跑 | `stalled` 归零，继续正常执行 |
| 事故回归（复刻 `task-2c8f8fbb...` 形状） | 终态 ∈ `{SUCCEEDED, FAILED, NEEDS_REVIEW}`，且 60 秒内不再变化 |

#### 回退
删除 `_finalize_needs_review` 的两处调用，恢复原挂起行为。

---

### P1-3 终态权限矩阵

#### 目标
明确"谁能把任务置为哪个终态"，防止乱标成功。

#### 改动：`kernel.py` 新增常量与守卫

```python
#: Who may drive a Task into each terminal state.
#: model  = a final answer / blocker report.
#: runtime = limits, stall caps, verification.
#: user   = explicit cancel.
TERMINAL_AUTHORITY: Mapping[TaskState, frozenset[str]] = {
    TaskState.SUCCEEDED: frozenset({"runtime"}),      # 只能经 VERIFYING→FINALIZING
    TaskState.FAILED: frozenset({"runtime"}),
    TaskState.CANCELLED: frozenset({"user", "runtime"}),
    TaskState.NEEDS_REVIEW: frozenset({"runtime"}),
}
```

在 `transition_task`（`kernel.py:8280` 起）里，对 `target.is_terminal` 增加 `authority`
参数校验；调用方显式传 `authority="runtime" | "model" | "user"`，
`Cancelled` 由用户面传入 `"user"`。

> 这是防御性改动：默认参数 `authority="runtime"`，因此历史调用点不需要修改；
> 用户取消路径（`CancelTaskInput` → `transition_task(CANCELLED)`）显式传 `"user"`。

#### 测试
| 用例 | 断言 |
|---|---|
| 以 `authority="model"` 尝试 `SUCCEEDED` | `InvalidTurnState` |
| `CancelTaskInput` | 成功且 authority 记录为 `user` |

---

## 7. P2：判断型判据（`rubric` + 有界评审员）

### P2-1 `rubric` 判据真正生效

#### 目标
让主观要求有一条"正路"，而不是逼 Planner 造假。

#### 改动 1：新增 Port

新文件 `src/tsm_agt/ports/rubric_judge.py`：

```python
class JudgeVerdict(StrEnum):
    SATISFIED = "satisfied"                    # 满足
    NEEDS_REVISION = "needs_revision"          # 不满足，可返工
    UNDECIDABLE = "undecidable"                # 判不了（量表不可评估）
    JUDGE_ERROR = "judge_error"                # 评审员自身出错（fail-closed）


@dataclass(frozen=True, slots=True)
class RubricJudgement:
    criterion_id: str
    verdict: JudgeVerdict
    reason: str = ""


class RubricJudgePort(RuntimeAdapter, Protocol):
    async def judge(self, criterion_id: str, assertion: str,
                    evidence_refs: tuple[str, ...]) -> RubricJudgement: ...
```

#### 改动 2：Adapter

新文件 `src/tsm_agt/adapters/model_rubric_judge/judge.py`，
实现要点：
- 独立模型调用（可配置为更便宜的模型），`temperature=0`；
- 输入只给：判据描述 + 本轮工具结果摘要 + 引用列表；
- 输出严格 JSON `{verdict, reason}`，解析失败 → `UNDECIDABLE`；
- 崩溃 → `JUDGE_ERROR`；
- 每次调用写事件 `rubric.evaluated`（含 attempt 序号）。

#### 改动 3：接入 `verify_task_acceptance`

在 `kernel.py:8429` 的 `verify_task_acceptance` 中，遍历 `TaskSpec.acceptance_criteria`
时对 `RUBRIC` 分支：

```python
elif criterion.verification_kind is TaskCriterionKind.RUBRIC:
    judgement = await rubric_judge.judge(
        criterion.criterion_id, criterion.description, shared_observation_refs,
    )
    passed = judgement.verdict is JudgeVerdict.SATISFIED
    criteria.append(AcceptanceResult(
        criterion.criterion_id,
        AcceptanceStatus.PASSED if passed else AcceptanceStatus.BLOCKED,
        (Evidence("rubric", criterion.description, judgement.reason,
                  "rubric-judge", passed),),
    ))
```

有界：每个判据最多 `max_judge_attempts`（默认 2）；
`UNDECIDABLE`/`JUDGE_ERROR` → `BLOCKED`（fail-closed）→ 任务 `FAILED`（可见），**不挂起**。

#### 改动 4：轮内门不产生 rubric gap

`_completion_readiness_gaps` 明确 `continue` 掉 `RUBRIC` 判据（与 `ANSWER` outcome 同理），
避免主观判据进入轮内阻塞。

#### 测试
| 用例 | 断言 |
|---|---|
| rubric 满足 | `PASSED` → 任务 `SUCCEEDED` |
| rubric 不满足 | `BLOCKED` → 任务 `FAILED`（不是 `AWAITING_USER`） |
| judge 抛异常 | `JUDGE_ERROR` → `FAILED`（fail-closed） |
| rubric 判据 | 轮内门产出 **0** 个必需 gap |

#### 回退
`rubric` 判据退化为"只记录不评估"（等价 P0 行为）。

---

## 8. P3：人工复核的收尾

### P3-1 `NEEDS_REVIEW` 的人工三选项

#### 改动
1. `task.py`：`NEEDS_REVIEW` 增加出边 `{EXECUTING, CANCELLED, SUCCEEDED}`。
2. `kernel.py` 新增：

```python
async def resolve_needs_review(
    self, task_id: str, decision: str, *, reason: str,
) -> TaskSnapshot:
    """decision ∈ {accept, return_for_revision, cancel}. Human authority only."""
```

- `accept` → `SUCCEEDED`，并写 `verification_status = "manual_verified"`；
- `return_for_revision` → `EXECUTING`，把未满足判据作为 steering 文本回注；
- `cancel` → `CANCELLED`。

3. `web/app.py` 新增 `POST /tasks/{id}/review`，任务卡片渲染三个按钮。

#### 事件
- `task.review_resolved` `{task_id, decision, reason, actor:"user"}`

#### 测试
| 用例 | 断言 |
|---|---|
| `accept` | `SUCCEEDED` + `verification_status=manual_verified` |
| `return_for_revision` | `EXECUTING`，且回注文本含未满足判据 id |
| `cancel` | `CANCELLED` |
| 对非 `NEEDS_REVIEW` 任务调用 | `InvalidTurnState` |

---

## 9. 兼容性、迁移与回退

### 9.1 必须遵守的兼容规则

| 规则 | 原因 |
|---|---|
| **校验只在作者期，不进 `__post_init__`/`from_data`** | 事故任务历史事件含 `turn-...`，否则回放失败 |
| 新增枚举值一律追加，不重排 | 事件快照按字符串存储；重排无影响但保持习惯 |
| 不改 `CompletionGap` / `AcceptanceResult` 的既有字段语义 | 旧测试与事件断言依赖 |
| 新增事件类型只增不改 | 回放器对未知事件应忽略；实施前确认 `TaskSpecProjector` 等投影器对未知事件安全 |
| `rubric` 判据在 P0/P1 期间只记录、不阻塞 | 允许 P0 先发布 |

### 9.2 迁移

- **历史数据**：无需迁移。`rubric` 是新增值；`NEEDS_REVIEW` 是新增状态。
- **历史任务**：保持原状态；不会自动重判。可用离线脚本按新规则重新评估（非本 SPEC 范围）。
- **阈值**：`completion_max_stalled_continuations` 默认 2，与
  `RuleBasedCompletionReadinessPolicy(max_stalled_continuations=2)` 一致。

### 9.3 回退

| Phase | 回退方式 |
|---|---|
| P0 | 还原 `_demote_unresolvable_criteria` 调用、`required=False`、`raise`、resume 端点 |
| P1 | 保留 `NEEDS_REVIEW` 枚举但不产生；移除 `_finalize_needs_review` 调用 |
| P2 | `rubric` 判据退化为"只记录" |
| P3 | 移除 review 端点与出边 |

**运营逃生阀**：发布期间可临时设置 `TSM_AGT_COMPLETION_READINESS_MODE=OBSERVE_ONLY`
（`bootstrap/composition.py:477-490`）让轮内门只诊断不阻塞。代价是同时关掉所有硬门，**只作临时手段**。

---

## 10. 测试总清单与回归基线

### 10.1 新增测试文件

| 文件 | 覆盖 |
|---|---|
| `tests/test_task_spec_criteria_validation.py` | P0-1 |
| `tests/test_completion_stall_terminal.py` | P1-2 |
| `tests/test_task_lifecycle_needs_review.py` | P1-1 |
| `tests/test_rubric_judge.py` | P2-1 |

### 10.2 追加用例

| 文件 | 覆盖 |
|---|---|
| `tests/test_task_spec.py` | P0-1 历史快照回放安全 |
| `tests/test_completion_readiness.py` | P0-2 / P0-3 |
| `tests/test_sdk_runtime.py` | P0-4 / P1-1 |
| `tests/test_web_phase1_state.py` | P0-4 / P1-1 |

### 10.3 事故回归（锁定形状）

```python
async def test_incident_turn_reference_cannot_deadlock(self):
    """task-2c8f8fbb75714516a94ab442995626b5 的形状：Planner 写 turn-... 引用。

    断言：
      1. 该判据被降级为 rubric（不再 required）；
      2. 不产生 required=True 的 task-spec:* gap；
      3. 任务终态 ∈ {SUCCEEDED, FAILED, NEEDS_REVIEW}；
      4. 终态在若干次续跑后不再变化（无活锁）。
    """
```

### 10.4 反向断言（防止矫枉过正）

```python
async def test_required_command_criterion_still_blocks(self):
    """降级规则不得放过真正的机器判据。

    一个 workspace_integrity 判据在 mutation 漂移后必须仍然产生
    required=True 的 gap，并允许通过重新写入修复。
    """
```

```python
async def test_no_complete_without_any_execution_fact(self):
    """降级后不得出现"无任何执行事实却判定 COMPLETE"。"""
```

### 10.5 基线

修订时全量套件基线以实施当日 `pytest -q` 为准（`完成判定改造SPEC.md §7.5` 记录为
`1023 passed, 8 skipped`，另有 9 个既有失败与本 SPEC 无关）。
**新增默认值与事件不得改变既有基线。**

---

## 11. 风险与未决项

| # | 风险 / 未决 | 处理 |
|---|---|---|
| 1 | `rubric` 与"只能由人判断"的边界 | P2 先只做"有界评审 + 可交人"，不做自动改写需求 |
| 2 | 五轴正交模型（完成/权威/中断/资源/外部）未落地 | P1 只加 `NEEDS_REVIEW`；五轴重构需单独立项 |
| 3 | 合同执行期被修订（`task_spec.revised`） | 本 SPEC 未改 `decide` 的进展比较；登记为 P2 之后的独立项 |
| 4 | `EventSpecProjector` 对未知事件的安全性 | P0 实施前先跑一次"注入未知事件"的回放测试 |
| 5 | `deterministic` 与 judge 的幂等 | judge 结果写事件，回放时读取而非重算 |
| 6 | 并行工具批次下的 gap 粒度 | 迟于本 SPEC；`完成判定改造SPEC.md §12.4` 已登记 |

---

## 12. 实施顺序 Checklist

```
[ ] P0-1  判据作者期校验 + 降级
[ ] P0-1a 确认 TaskSpecProjector 对 task_spec.criteria_demoted 未知事件安全
[ ] P0-2  TASK_SPEC_EVIDENCE → advisory + 不变量防守
[ ] P0-3  判定器异常 fail-closed（含诊断调用点包裹）
[ ] P0-4  CONTINUATION resume 通道（SDK + Web + 前端按钮）
[ ] P0    跑事故回归 + 反向断言 + 全量基线
──────────  发布 P0  ──────────
[ ] P1-1  NEEDS_REVIEW 终态（task.py / SDK / Web）
[ ] P1-2  停滞到顶 → _finalize_needs_review
[ ] P1-3  终态权限矩阵
[ ] P1    跑活锁回归 + 全量基线
──────────  发布 P1  ──────────
[ ] P2-1  rubric 判据 + RubricJudgePort + 有界评审
[ ] P2    跑 judge 失败/判不了 用例
──────────  发布 P2  ──────────
[ ] P3-1  NEEDS_REVIEW 人工三选项 + Web 端点
[ ] P3    跑人工路径用例
──────────  发布 P3  ──────────
```

---

## 13. 验收口径（整体）

1. 规划期不再有非法 `evidence_reference` 落库（P0-1 测试）。
2. 不存在"必需且不可闭合"的 gap（P0-2 断言 + 全库搜索）。
3. 判定器异常不产生 `SUCCEEDED`（P0-3 测试）。
4. CONTINUATION 有结构化 resume 入口（P0-4 接口测试）。
5. 不可闭合的必需工作最终进入 `NEEDS_REVIEW`，不再无限 `AWAITING_USER`（P1-2 测试）。
6. 终态由授权角色产生（P1-3 测试）。
7. 主观判据走 `rubric` 且有界、可失败、可交人（P2 测试）。
8. `NEEDS_REVIEW` 有人工三选项闭环（P3 测试）。
9. 历史事件（含事故任务）可回放且不抛异常（P0-1 回放测试）。
10. 全量测试基线不退化。

---

## 14. 英文术语表

| 英文 | 中文 | 解释 |
|---|---|---|
| authoring time | 作者期 | 判据被写进合同的时刻（closing/planning 时） |
| demote | 降级 | required → advisory |
| advisory | 提示性的 | 只记录，不阻塞 |
| closable | 可闭合的 | 有办法从不达标变为达标 |
| fail-closed | 失败即关闭 | 出错时默认"不通过" |
| fail-open | 失败即放行 | 出错时默认"通过"（缺陷 D5） |
| terminal state | 终态 | 不再自动变化 |
| replay | 回放 | 用历史事件重建状态 |
| invariant | 不变量 | 任何时候都必须成立的规则 |
| bounded | 有界的 | 有次数上限 |
| stall | 停滞 | 未解决集合没有变化 |
| rubric | 评分量表 | 判断型判据的评估标准 |
| judge | 评审员 | 按 rubric 出结论的模型或人 |
| verdict | 结论 | judge 的输出 |
| undecidable | 判不了 | 量表不可评估 |
| authority | 权限 | 谁有权设置某状态 |
| regression | 回归 | 锁定历史缺陷的测试 |
| baseline | 基线 | 全量测试的通过数基准 |
| rollout | 发布推进 | 分批上线 |
| rollback | 回退 | 撤销改动 |

---

## 15. 实施记录与偏差（P0–P3 已实施）

实施日期：2026-09-26
结果：全量 `pytest -q` → **1080 passed / 9 failed（均为既有失败）/ 8 skipped**，
新增测试全部通过。

### 15.1 与本文档的偏差（重要）

| # | 文档原设计 | 实际实施 | 原因 |
|---|---|---|---|
| 1 | P0-1 在 `plan_task_spec` 里对非法引用 **raise**，触发 Planner bounded correction | **降级**（不 raise）；硬拒绝只保留在 `revise_task_spec`（模型工具路径） | 实测发现：Planner 连续两次给出非法引用时 raise 会让**整条任务规划失败**，比降级更糟。降级保证任务继续，且永不产生不可闭合缺口。 |
| 2 | P0-1 `MACHINE_REFERENCE_PREFIXES` 只用于 `validate_authored_reference` | 同时从 `tsm_agt.core` 导出 | 测试与调用方便 |
| 3 | （文档未覆盖）`verify_task_acceptance` 对 RUBRIC 的处理 | **必须显式 `continue`/走 judge** | 否则降级后的 RUBRIC 判据因 `evidence_reference=None` 被 `_verify_task_spec_reference` 判为 `invalid_reference` → **任务被误判 FAILED**。这是实施中发现的第二个同类缺陷。 |
| 4 | P0-2 曾考虑改 `_completion_readiness_gaps` 返回值以携带违规列表 | 保持返回值不变，原地降级 + `completion.invariant_violated` 事件 | 该函数有 3 个内部 + 6 个测试调用点，改签名破坏面过大 |
| 5 | P0-4 需修正 `_waiting_payload` 的 `kind` | **无需修改**：`clarification.kind` 本来就是 `"CONTINUATION"` | 文档基于错误前提；实际只需加端点与前端按钮 |
| 6 | P1-2 由 `_suspend_incomplete_recoverable` 的 `stalled >= cap` 触发 | 该路径存在，但预算续期会重置 `stalled`；**可靠触发点是 `resume_agent_continuation` 的入口守卫** | 两处都已实现并测试；守卫是确定性的 |
| 7 | P1-1 只加 TaskState | 另加 `Phase1TaskState.NEEDS_REVIEW` 与 `TaskDisplayStatus.NEEDS_REVIEW` | 投影与前端需要区分显示 |
| 8 | P1-3 `authority` 需在 CANCELLED 路径传 `"user"` | 未改任何调用点（默认 `"runtime"`，CANCELLED 允许 runtime） | 防御性矩阵，零行为变化 |
| 9 | P3 accept 写 `verification_status = "manual_verified"` | 写 `verify.completed` 事件并带 `manual_verified: True`，status 仍为 `passed` | 不新增 session 投影的枚举值，保持兼容 |
| 10 | P1 阶段 NEEDS_REVIEW 无出边 | P3 已开放人工出边 `{EXECUTING, CANCELLED, SUCCEEDED}` | P3 实施完成；`is_terminal` 仍为 True（表示"无自动续跑"，人工可重开） |

### 15.2 关键不变量的落地证据

- **INV-2 / INV-3**：`task-2c8f8fbb...` 形状的 `turn-...` 引用被降级为 `rubric`，
  不再产生 `required=True` 的 `task-spec:*` gap
  （`tests/test_task_spec_criteria_validation.py`）。
- **INV-4 / INV-6**：不可闭合必需判据最终进入 `NEEDS_REVIEW`，不再无限 `AWAITING_USER`
  （`tests/test_completion_stall_terminal.py`）。
- **INV-5**：判定器异常 → `completion.readiness_failed.fail_closed = True`，不产生 `SUCCEEDED`
  （`tests/test_completion_readiness.py::AcceptanceGateInvariantTest`）。
- **P0-4**：SDK `resume_continuation` + `POST /tasks/{id}/resume` + 前端"继续"按钮。
- **P2**：`RUBRIC` 由 `ModelRubricJudge` 有界评审；无 judge 时非阻塞
  （`tests/test_rubric_judge.py`）。
- **P3**：`POST /tasks/{id}/review` 支持 accept / return_for_revision / cancel。

### 15.3 已知未关闭项

1. **9 个既有失败**与本 SPEC 无关（PTY 5、composition 2、dependencies 1、investigation 1），
   实施前即存在。
2. `stalled` 计数在预算续期路径上会被重置——当前由 resume 入口守卫兜底；
   若要彻底修，需单独立项（见 §11 风险 3）。
3. 五轴正交模型（完成/权威/中断/资源/外部）未落地，本 SPEC 只加了 `NEEDS_REVIEW`。
4. `return_for_revision` 只队列 steering 事件，未自动触发新一轮 Turn；
   客户端需要显式继续（与 P0-4 的 resume 通道一致）。

