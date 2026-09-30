# tsm-agt 恢复状态清单（`git reset --hard` 事故）

> 本文替代同名旧版。旧版写于事故刚发生时，其中三条信息已经过期：
> ①「需要恢复 29 个文件」②「验收标准是 `1456 passed`」③「HEAD 依赖 7 个未跟踪文件」。
> 现在工作区相对 HEAD 全干净、HEAD 自包含、全量 0 失败，但**仍有东西找不回来**——
> 第 4 节是本文最重要的一节。

---

## 0. 现状（一句话）

`HEAD` 自包含（干净导出可导入、可跑全量），全量 **1429 passed / 13 skipped / 0 failed**；
13 个 skip 与事故前完全一致。但**事故前有 1469 个用例，现在只有 1442 个**——
**28 个用例只存在于你的备份里**，绿色测试测不出它们的缺失（第 4 节）。

---

## 1. 事故与我的责任

为修改三个提交信息里写错的行数，我执行了 `git reset --hard`。该命令丢弃**工作区
未提交的改动**，而这个仓库在会话开始前有 32 个跟踪文件带着未提交改动。

- git 内恢复途径已穷尽：无 stash、无含该内容的悬空 blob/tree/commit、所有分支与
  远程均从未提交过、`__pycache__` 已在 21:03 按还原后的源码重编。
- 后果一度是 138 个 pytest 收集错误、包不可导入。
- 更深的根因：`git add <path>` 会**吸收该文件当时全部既有改动**，所以当时的提交
  是半一致的（例如 `7b320b4` 提交了 `ports/__init__.py` 的引用，而 `ports/tool.py`
  里的定义留在工作区未提交）。这是我该在动手前就想清楚的。
- **我没有重写任何历史来掩盖它。** 全部修复都是新增提交。

---

## 2. 现在已验证的状态

| 检查 | 命令 | 结果 |
|---|---|---|
| 工作区干净 | `git status --short` | 无输出 |
| HEAD 自包含 | `git archive HEAD \| tar -x -C /tmp/x && cd /tmp/x && pytest -q` | `1429 passed, 13 skipped` |
| 全量 | `.venv/bin/python -m pytest tests/ -q` | `1429 passed, 13 skipped, 4 warnings, 307 subtests passed` |
| 架构约束棘轮 | `pytest tests/architecture -q` | 通过（含 `test_kernel_facade.py` 5 条门禁） |

事故前实测为 `1456 passed, 13 skipped`。**13 个 skip 对上了**，这很重要：它证明
13 个里没有任何一个是因为错误而被跳过的。

---

## 3. 事故后的恢复提交（全部为新增提交，无历史重写）

| 提交 | 内容 |
|---|---|
| `3ae14a2` | 重建 5 处丢失符号 —— 138 个收集错误 → 51 个失败 |
| `a9b6a27` | `ModelConfiguration.review_model`（SPEC §12.6 D5）—— 51 → 30 |
| `7533619` | `ToolCall.evidence_intent` + `code.diagnostics` 的 `ENUMERATE_SCOPE` —— 30 → 26 |
| `f5f80be` | `RubricJudgement.cited_evidence_ids` + 判官自动归属 —— 26 → 23 |
| `c624f86` | `pending_steering` 收据 + 孤儿 Task 先 settle + rubric packet 必含 goal —— 23 → 19 |
| `d0c5e19` | `pending_steering` 在 `to_data()` 里缺席省略（我自己的回归）—— 19 → 18 |
| `8f9247b` | 按 SPEC §4.3 纠正 `WORKSPACE_INTEGRITY` 与 `event:`/`mutation:` 的编译 —— 18 → 10 |
| `6c8a2dd` | 孤儿 Task 判定只做一次，不再把消息报成 `steered` |
| `40b3c18` | 孤儿判定与 runner 注册共用 `_submit_lock`，堵住"取消正在启动的任务"竞态 |
| `843b391` | 补回 planner prompt 丢失的三条契约句 |
| `f98bb2f` | `local_sandbox` 恢复路径规范化语义（且不使用 `.resolve()`） |
| `98a1dcf` | 恢复 pty 可用性守卫，5 个 pty 用例回到 skip |
| `c45f376` | 把 9 个「HEAD 依赖但从未提交」的源文件入库 |
| `579e36a` | 把 4 个「从未提交」的回归测试入库 |
| `1bc4bed` | 本文入库 |

---

## 4. ⚠️ 绿色测试**不能**证明恢复完整：还有 28 个用例缺失

这是唯一还需要你动手的一件事。

| | 收集到的用例 | passed | skipped |
|---|---|---|---|
| 事故前 | **1469** | 1456 | 13 |
| 现在 | **1442** | 1429 | 13 |
| 差额 | **−27** | | |

现在这 1442 里包含**我本轮新增的 1 个**用例（`test_task_being_started_is_not_settled_as_an_orphan`）。
所以：

> **1469 − (1442 − 1) = 28 个用例在事故中丢失。**

它们来自 29 个丢失改动里的**测试文件**：事故把工作区版本冲回 HEAD 版本，而 HEAD
版本比工作区版本**少**这些用例。**一个变小的测试集照样全绿**，所以 `0 failed`
完全掩盖了这件事——这就是为什么我必须把这条单独列出来。

受影响的测试文件（丢失的改动里可能新增了用例或断言）：

```
tests/test_cli_line_editing.py
tests/test_completion_readiness.py
tests/test_final_acceptance.py
tests/test_goal_alignment.py
tests/test_local_workspace_sandbox.py
tests/test_post_mutation_applicability.py
tests/test_production_journeys.py
tests/test_rubric_judge.py
tests/test_runtime_task_result.py
tests/test_task_runtime_projection.py
tests/test_task_spec_criteria_validation.py
```

### 怎么用你的备份核对

```bash
# 备份里的用例总数；应当是 1470（事故前 1469 + 我新增的 1）
<python> -m pytest <备份目录>/tests --collect-only -q | tail -1

# 逐文件比对用例数，差值就是缺失的用例在哪
for f in tests/test_*.py; do
  printf '%-52s ' "$f"
  <python> -m pytest "$f" --collect-only -q 2>/dev/null | tail -1
done
```

**只把测试文件从备份里取回来即可**（源码已经不需要了：源码侧的失败已全部修掉，
且从证据重建的部分都在上面的提交里）。取回后如果出现新的失败，把失败列表发我，
我按失败逐个定位——**只做证据驱动的判断，不猜**。

---

## 5. 源码侧：哪些是重建的、依据是什么

源码的 29 个丢失文件现在**行为**上已被测试覆盖并全绿，但「全绿」不等于「与丢失
版本逐字相同」。重建时用的权威依据，供你审阅：

| 丢失内容 | 重建依据 |
|---|---|
| `EvidenceCapability` 8 个成员、`(effect, result_authority)` 派生、`extra_evidence_capabilities`、冲突时启动期 fail-closed | 已删 SPEC 原文：`git show '6f2a870^:Artifact Graph与Proof Obligation任务证明闭环实施SPEC.md'` §4.3 |
| `ProofPlanCompiler` 的判据映射（`WORKSPACE_INTEGRITY` → `EFFECT_OBSERVED`、`event:` → `ARTIFACT_EXISTS` 且 selector 用 `locator`、`mutation:` → `EFFECT_OBSERVED`、`tool_call:` 需已记录 capability） | 同一份 SPEC §4.3 第 475-484 行，逐行核对 |
| `RuntimeTaskResult.pending_steering`、`RubricJudgement.cited_evidence_ids`、`ToolCall.evidence_intent`、`review_model` | 幸存测试里这些字段被消费的确切形状 |
| `local_sandbox` 的路径规范化 | git 历史本身：`983b77d` 用 `.resolve()`，`a2a9c1e` 换成 `.absolute()` 而丢了语义 |
| pty 用例的跳过守卫 | skip 数算术：13 − 8 = 5（第 3 节末） |

### 完全没有测试覆盖的丢失改动 —— 这部分只能靠你的备份

- **`src/tsm_agt/cli.py`**（3,905 行）
- **`src/tsm_agt/web/app.py`**（3,843 行）

这两个文件体量大、UI 性质、本仓库的测试几乎没有触达。如果备份里这两个文件与
HEAD 有差异，**优先用备份覆盖它们**（前提是先按第 6 节备份当前状态）。

---

## 6. 覆盖备份前的安全动作（已做成脚本）

**不要手敲 29 次 `cp`。** 用 `scripts/restore_from_backup.sh`，它默认只预览：

```bash
# ① 预览：逐文件打印「当前 HEAD 版本 vs 备份版本」的差异行数，不写任何文件
scripts/restore_from_backup.sh <备份根目录>

# ② 确认差异方向正确（备份不应比当前更旧）后，才真正恢复
scripts/restore_from_backup.sh <备份根目录> --apply
```

脚本的四个保证：

1. **默认只预览**，不加 `--apply` 绝不写文件。
2. **只碰清单里的 29 个文件**，并明确跳过第 1 节末尾那 3 个「勿覆盖」文件。
3. 先检查备份根目录确实含 `src/tsm_agt/core/kernel.py`，避免路径拼错后到处乱拷。
4. `--apply` 前自动打标签 `pre-restore-<HEAD>`，且要求**已跟踪文件干净**
   （未跟踪文件不拦），排除恢复后分不清差异来源的情况。

**回退方式（关键）**：我这轮全部改动都已提交，所以任何被覆盖的文件都能原样拿回：

```bash
git checkout HEAD -- <路径>
# 或整体回到恢复前：
git checkout pre-restore-<HEAD> -- .
```

已实测的完整流程：预览 → `--apply` → `git checkout HEAD -- <路径>` 回退 →
工作区干净、全量仍 `1429 passed / 13 skipped`、无遗留标签。

恢复后的验收命令脚本会直接打印，核心四条：

```bash
# ① 用例数应回到 ~1470（事故前 1469 + 本轮新增 1）
.venv/bin/python -m pytest tests/ --collect-only -q | tail -1

# ② 全量
.venv/bin/python -m pytest tests/ -q

# ③ 主导失败模式的产码应出现（此前只有 turn_outcome.py 的识别列表有这串）
grep -rn "verifier blocked" src/ --include=*.py

# ④ 真库的 EXECUTING 数应从 3 降到 0（先看 §8.3 的扫描范围）
```

---

## 7. 顺带修掉的两个结构性风险

1. **HEAD 曾经不是自包含的。** 已提交的 `core/kernel.py`、`core/__init__.py`、
   `core/task_spec.py`、`core/kernel_tool.py` 直接 import 9 个从未提交过的模块，
   一次 `git clean` 就能让整包不可导入——这正是"138 个收集错误"能发生的前提。
   现已全部入库（`c45f376`、`579e36a`），并用干净导出验证过。

2. **两个模块目前只有测试消费者，未接入生产组合**（按原样入库存档，未擅自接线，
   因为接线需要原始设计意图）：
   - `core/turn_finalizer.py` —— 仅被 `tests/test_proof_closure.py:2429` 引用
   - `adapters/builtin/todo_tools.py` —— `adapters/builtin/__init__.py` 未注册它

   这两个是否应该接进组合，需要你确认。

---

## 8. 用真实台账核对后的三个发现（只读副本，原库未改动）

方法：用 SQLite 自己的 `backup()` API 把 `.agent/runtime.db`（447 MB）复制到 `/tmp` 再分析，
原库全程只读。真实规模：77 Session / 687 Task / 114,840 事件。

### 8.1 主导失败模式是「无法判定被写成失败」，占真实的 71%

真实 687 个 Task：`SUCCEEDED 446` / `FAILED 161` / `AWAITING_USER 32` /
`INTERRUPTED 23` / `NEEDS_REVIEW 11` / `CANCELLED 7` / **`EXECUTING 3（永久卡住）`** /
`AWAITING_APPROVAL 2` / `CONFLICT 2`。

161 个 FAILED 的收尾原因：

```
69  trusted verifier blocked
46  SDK verifier blocked          <- 合计 115 = 71% 的真实失败
15  SDK background execution failed
11  model invocation failed
11  SDK verifier failed
 4  max_model_calls
 ...
```

实际事件序列是 `VERIFYING --"trusted verifier blocked"--> FAILED`（已逐条打开确认）。

而 `core/turn_outcome.py` **已经判定这不是失败**：

```
derive_turn_outcome(FAILED, "SDK verifier blocked")
  -> completed=False, failed=False, interrupted=True, exit_reason='undecidable'
```

你自己的测试注释就写着这件事（`tests/test_proof_closure.py:2372`）：
`# The exact wording from the ledger: 69 + 46 tasks reached FAILED this way.`
—— 与我独立从台账数出来的 69 + 46 完全一致。

### 8.2 但这个修复**没有接进生产**

```
derive_turn_outcome        无生产消费者
outcome_inconsistencies    无生产消费者   (它能报 FAILED_STATE_WITH_UNDECIDABLE_REASON)
classify_transition_reason 无生产消费者
finalize_turn              无生产消费者
```

四个都只被 `tests/test_proof_closure.py` 引用。所以运行时仍然把「无法判定」写成 `FAILED`，
这就是「不知道如何完成闭环」在台账里的样子。

**接线正是被销毁的那批未提交改动在做的事**：HEAD 源码里**没有任何地方生产**
`"SDK verifier blocked"` / `"trusted verifier blocked"` 这两个字符串（只有
`turn_outcome.py` 的识别列表里作为子串出现）。产码在 `sdk/runtime.py` / `cli.py` 里，
而这两个文件都在丢失清单上。→ **恢复备份很可能就把这一步带回来了，不必我从头接线。**

### 8.3 三个真实 Task 永久卡在 EXECUTING，且启动期 reconcile 够不到

```
task-c83385c6ba8447868cbd596f11b58cb0  EXECUTING  查下今天的nba新闻
task-5711c97fe5244a388779fa855ee6c049  EXECUTING  帮我看下当前项目有哪几个文件夹 分别是啥功能
task-9998de4ddd134474825800c8a01ed435  EXECUTING  必须使用可用的只读工具读取 src/tsm_agt/core/pol...
```

它们的 3 个 Session 的 `subject = 'legacy-local-subject'`，而当前身份是 `uid:501`
（77 个 Session 里 74 个是 `uid:501`）。会话查询按 subject 过滤：

```
list_sessions(include_archived=True) -> 74   （不是 77）
get_session(legacy_session)          -> PermissionError: session belongs to another local subject
reconcile_orphaned_running_tasks()   -> settled 0
settle_orphaned_task(task)           -> True   （单独调用是成功的！）
```

所以问题出在**扫描范围**，不在 settle 逻辑。这三个 Session 建于 2026-08-31，
恰好是身份从占位符 `legacy-local-subject` 切到 `uid:501` 之前。

---

## 9. 一句话结论

代码层面已修到全量绿、干净导出可跑、真实 socket 的模型 HTTP 往返也实测通过；
但**「真实场景能正常用」还不能确认**，差三件事：§8.2 的接线（或等备份恢复）、
§8.3 的扫描范围、以及本沙箱无法访问模型端点（DNS 把
`aibrain-large-model-engine-slow.hellobike.cn` 解析到 `198.18.0.71` 黑洞，
`danger-full-access` 下依然如此）。

---

## 10. 需要你在**有伪终端**的普通终端里验证的一件事

```bash
.venv/bin/python -m pytest tests/test_cli_line_editing.py -q
```

本沙箱禁 `/dev/ptmx`（`os.openpty()` → `PermissionError`），所以这 5 个用例在这里
只能 skip。守卫只跳过"根本无法分配伪终端"的环境；正常终端/CI 上它们照常运行并
断言。**如果你希望连"跳过"都不允许，那就把备份里这一份测试文件取回来。**
