# tsm-agt 工具边界能力 vs Hermes（决定 transcript 能否收录 tool 行）

> **行号引用已失效（2026 更正中）**：本文写作时 `core/kernel.py` 尚未分解。
> 此后轮循环已抽为 `core/turn_phases.py` / `core/turn_tool_batch.py`，
> 并按 Hermes 门面+Mixin 形状开始分解（第 1 刀 `core/kernel_session.py`，
> 见 `tsm-agt-按Hermes重构kernel映射.md`）。文中 `kernel.py:NNNN` 一律不再
> 指向所述代码；请按同处已写出的**方法名**重新定位。
> 保留原行号是为了让这份记录仍可追溯到当时的代码。

> 问题：能不能像 Hermes 那样，把 **tool 行**（含工具结果）纳入会话记录？
> Hermes 敢全录，是因为它在**工具边界**有兜底。本文逐项核实 tsm-agt 是否有等价能力。
> 全部数字为实测（`wc -l`）。

---

## 1. 结论（先给答案）

**不能按 A 路全录 tool 行。** 理由：Hermes 用来兜底的三样东西，tsm-agt **缺两样、弱一样**：

| 兜底机制 | tsm-agt | Hermes | 判定 |
|---|---|---|---|
| 工具结果**内容脱敏** | ❌ **无** | `agent/redact.py` **1354 行** | **缺** |
| 结果**截断强制** | ❌ 字段声明后**无人消费** | `tool_output_limits.py` + `tool_output_truncate.py` + `registry.get_max_result_size()` | **缺** |
| **审批** | ~530 行 | ~4,300 行 | **弱（1/8）** |
| **沙箱** | 1 个本地沙箱 374 行 | 6 个后端（local/docker/ssh/modal/daytona/singularity/vercel） | **弱** |
| 副作用**声明**元数据 | `risk` R0-R4 + `effect` + `result_authority` + `idempotency` | 仅 `effect_disposition` 一个字符串 | **强** ← 唯一优势 |

**⇒ 推荐 B 路**：transcript 先只收 **user / assistant 文本行**，让"用户说过什么"跨轮存活；tool 行待边界补齐后再录。

---

## 2. 逐项证据

### 2.1 内容脱敏：tsm-agt **没有**

全仓搜 `redact`，命中全部是**配置/状态的展示脱敏**，没有一处作用于工具结果：

| 位置 | 实际作用 |
|---|---|
| `cli_setup.py:342` | `text.replace(secret, "<redacted>")` —— 只在 CLI 打印配置时 |
| `core/configuration.py:1` | docstring：*"Immutable, **redacted** Effective Configuration snapshots"* |
| `kernel.py:4301,7234` | *"Return a **redacted** Session-level projection"* / *"Rebuild redacted status"* —— 状态视图 |
| `flow.py:613` | 流程定义里的一个 `"redaction"` 键 |
| `agent_loop.py:111` | docstring：*"intentionally **not** a redacted audit projection"* |

**⇒ 工具结果进入模型上下文前，没有任何内容脱敏。**

对照 Hermes：`agent/redact.py`（1354 行）+ `tools/file_tools.py:16` 的 `_is_secret_file_arg, redact_sensitive_text`；`_redact_cdp_output`（`tools/browser_cdp_tool.py:48-76`）还会按路径标记逐层剥离。

### 2.2 结果截断：**声明了，但无人消费**

`grep -rn "max_result_tokens"` 的全部命中：

```
src/tsm_agt/ports/tool.py          ← 字段定义（默认 2000）
src/tsm_agt/core/configuration.py:129  ← 序列化进配置
src/tsm_agt/adapters/builtin/todo_tools.py:281      ← 声明
src/tsm_agt/adapters/builtin/interaction_tools.py:51 ← 声明
```

**没有任何一处读它并据此裁剪结果。** 排查过 `_execute_authorized_tool`（`kernel.py:16925`）与 `invoke_tool`（`:16330`）两条执行路径，也无截断逻辑。

**唯一的截断**是命令输出自带的标志（`kernel.py:1108,1114,4859,4864` 的 `stdout.truncated` / `stderr.truncated`）——那是**进程层**的限制，不是工具结果层的。

对照 Hermes：`tools/tool_output_limits.py`(64) + `tools/tool_output_truncate.py`(32) + `tools/budget_config.DEFAULT_RESULT_SIZE_CHARS`(114) + `ToolEntry.max_result_size_chars` + `registry.get_max_result_size()`（`registry.py:924-935`）。

### 2.3 审批：规模 1/8

| | tsm-agt | Hermes |
|---|---|---|
| 文件 | `core/approval.py` **131** | `tools/approval.py` **1313** |
| | `core/policy.py` 300 | `tools/approval_detection.py` **1540** |
| | `core/trust.py` 95 | `tools/approval_smart.py` 155 |
| | | `tools/approval_floors.py` 204 |
| | | `tools/approval_prompt.py` 336 |
| | | `tools/approval_context.py` 332 |
| | | `tools/approval_gateway_wait.py` 224 |
| | | `tools/approval_human_wait.py` 170 |
| **合计** | **~530** | **~4,300** |

tsm-agt 的 `core/approval.py` 是**纯数据类型**（`ApprovalDecision` / `ApprovalKind` / `ApprovalRequest` / `ApprovalRequired` / `ApprovalNotPending` / `ApprovalPayloadMismatch`），**没有命令风险检测**（Hermes 的 `approval_detection.py` 独占 1540 行）。

### 2.4 沙箱

* tsm-agt：`adapters/local_sandbox/sandbox.py` **374 行**，一个本地沙箱
* Hermes：`tools/environments/` **6+ 后端**（local/docker/ssh/modal/daytona/singularity/vercel），`docker.py` 单文件 1274 行

### 2.5 唯一优势：声明性元数据更强

```python
class ToolSpec:            # ports/tool.py:222
    risk: ToolRisk                    # R0~R4
    effect: ToolEffect                # OBSERVE/MUTATE/EXECUTE/INTERACT/CONTROL/INTERNAL
    result_authority: ToolResultAuthority
    idempotency: ToolIdempotency
    is_read_only / is_concurrency_safe / is_internal_state
    data_transmission / rollback
    extra_evidence_capabilities
```

Hermes 对应的只有一个字符串列 `messages.effect_disposition`。

**⇒ tsm-agt 能"声明"风险，但缺少"执行期兜底"。** 声明本身不能替代脱敏与截断：一个 R2 的 MUTATE 工具说自己是 R2，不会让它的输出里少一个密钥。

---

## 3. 对决策的直接影响

**B 路（先只录 user/assistant）为什么现在就能做**：
- 用户/助手的**文本**是双方有意表达的对话内容，`_session_task_summary`（现 `core/kernel_session.py`）已经证明作者认为这部分可以进会话上下文
- 不触碰 `_session_task_summary` 里那条明确的安全决策（其正文随方法搬到了 `core/kernel_session.py`）：*"Never copy command argv/environment, patch bodies or arbitrary third-party Tool arguments into Session context"*

**A 路（全录 tool 行）的前置条件**（按必要性排序）：

1. **强制 `max_result_tokens`** —— 字段已存在、已进配置，**只差一个消费点**。这是成本最低、收益最直接的一步（Hermes 等价物 ~200 行，tsm-agt 可更小）。
2. **引入工具结果脱敏** —— tsm-agt 完全没有，需要新建（Hermes 是 1354 行，但可按 `ToolSpec.effect`/`risk` 收敛到比 Hermes 更小的实现，因为 tsm-agt 的声明元数据更强）。
3. ~~命令风险检测~~ —— 可后置：tsm-agt 的 `risk` 声明 + 审批请求已能覆盖主要场景。

**⇒ 建议顺序**：B 路先把多轮接上 → 补 `max_result_tokens` 强制（独立可验）→ 补结果脱敏 → 再开 A 路录 tool 行。

---

## 4. 未核实项

1. `core/policy.py`(300) 与 `core/trust.py`(95) 的实际判定逻辑（只看了行数与文件名）。
2. tsm-agt 是否有**密钥作用域**机制（Hermes 有 `agent/secret_scope.py` 453 行；未在 tsm-agt 找到对应物，但也未系统排查）。
3. `local_sandbox/sandbox.py` 的实际隔离强度（是否限制文件系统/网络/进程）。
4. Hermes `agent/redact.py` 的完整脱敏规则（只确认了存在与调用点，未读规则表）。
5. 工具结果进入模型上下文的确切路径（`invoke_tool` → `ToolResult` → messages）上是否还有其他过滤。
