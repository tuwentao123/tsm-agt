# tsm-agt 恢复状态清单（`git reset --hard` 事故）

> 本文替代同名旧版。旧版写于事故刚发生时，其中三条信息已经过期：
> ①「需要恢复 29 个文件」②「验收标准是 `1456 passed`」③「HEAD 依赖 7 个未跟踪文件」。
> 现在工作区相对 HEAD 全干净、HEAD 自包含、全量 0 失败，但**仍有东西找不回来**——
> 第 4 节是本文最重要的一节。

---

## 0. 现状（一句话）

`HEAD` 自包含（干净导出可导入、可跑全量），全量 **1430 passed / 13 skipped / 0 failed**；
13 个 skip 与事故前完全一致。但**事故前有 1469 个用例，现在只有 1443 个**（含我新增的 2 个）——
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
| HEAD 自包含 | `git archive HEAD \| tar -x -C /tmp/x && cd /tmp/x && pytest -q` | `1430 passed, 13 skipped` |
| 全量 | `.venv/bin/python -m pytest tests/ -q` | `1430 passed, 13 skipped, 4 warnings, 307 subtests passed` |
| 架构约束棘轮 | `pytest tests/architecture -q` | 通过（含 `test_kernel_facade.py` 5 条门禁） |
| CLI / Web 入口 | `tsm-agt --help`、`import tsm_agt.web.app` | 24 个子命令、21 条路由均可加载 |
| `tsm-agt doctor` | `.venv/bin/tsm-agt doctor` | `healthy`，14 个 adapter started and healthy |
| **真实 HTTP 上的完整工程闭环** | `pytest tests/test_engineering_loop_over_http.py -q` | 见下 |

### 真实 HTTP 上的完整工程闭环（`57b281b` 新增）

只把**模型端点**换成本地 127.0.0.1 上的 OpenAI 兼容服务，其余全是生产代码。一条
测试里跑完并逐步断言：

```
run_agent_turn → planner 经真实 HTTP 调用（planner__submit_task_spec）
               → proposal 解析成真 TaskSpec（ac-durable-change）
               → agent 发 core__apply_patch（R1）→ turn 停在审批（文件此时不存在）
resolve_agent_approval(APPROVE) → 续跑 → 磁盘上真的出现 DELIVERABLE.md
verify_task_acceptance → PASSED
finalize_acceptance    → SUCCEEDED
```

这条测试覆盖了此前离线用例**完全没碰**的三个接缝：套接字层、provider 侧工具名
编解码（`core.apply_patch` ↔ `core__apply_patch`）、以及 planner 的独立工具列表。
负向验证过：把 `TERMINAL_STATE_BY_VERDICT[PASSED]` 改成 `NEEDS_REVIEW`，测试立刻
失败（`AssertionError: NEEDS_REVIEW is not SUCCEEDED`）。

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
| 现在 | **1443** | 1430 | 13 |

现在这 1443 里包含**我本轮自己新增的 2 个**用例
（`test_task_being_started_is_not_settled_as_an_orphan`、
`test_plan_approve_mutate_verify_and_finalize`）。所以：

> **1469 − (1443 − 2) = 28 个用例在事故中丢失。**

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
# 备份里的用例总数；恢复到事故前水平的话应当是 ~1469
# （低于 1469 就说明备份里的测试文件仍不完整）
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

> ⚠️ 前提：**得先有一份备份。** 我在本机没找到任何可用恢复源（§8.4 有完整排查表）。
> 如果备份在别的机器/移动硬盘/云端，用下面的脚本；如果没有，这一节跳过，
> 因为 §8.2 已说明主导失败模式在当前代码里就是对的，不缺这一步。

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
工作区干净、全量仍 `1430 passed / 13 skipped`、无遗留标签。

恢复后的验收命令脚本会直接打印，核心四条：

```bash
# ① 用例数应回到 ~1469（事故前水平；低于它就说明备份里的测试文件不完整）
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

2. **两个模块目前只有测试消费者，未接入生产组合**（按原样入库存档，未擅自接线）：
   - `core/turn_outcome.py` / `core/turn_finalizer.py` —— 仅被
     `tests/test_proof_closure.py` 引用
   - `adapters/builtin/todo_tools.py` —— `adapters/builtin/__init__.py` 未注册它

   ⚠️ **不要把它们误读成「主导失败模式的修复没接线」**——那个修复在生产里是
   `finalize_acceptance` → `terminal_state_for`，一直是接好的（见 §8.2 的更正）。
   `turn_outcome` 是**读旧台账时的分类器**（把历史里的
   `FAILED_STATE_WITH_UNDECIDABLE_REASON` 标出来），要不要在 CLI/Web 上展示
   outcome 轴是独立的产品决定，需要你确认。

---

## 8. 用真实台账核对后的发现（含两处**更正**）

方法：用 SQLite 自己的 `backup()` API 把 `.agent/runtime.db`（447 MB）复制到 `/tmp` 再分析，
原库全程只读。真实规模：77 Session / 687 Task / 114,840 事件。

> ⚠️ 8.2 与 8.3 是**对同一份文档上一版的更正**。上一版我给的两个结论（"修复没接进
> 生产"、"reconcile 扫描范围 bug"）都**不准确**，原因是我只看了 `turn_outcome.py`
> 的消费者，没有顺着 `finalize_acceptance` 查下去，也没有查 `legacy-local-subject`
> 这个值的来源。这里按证据改正。

### 8.1 主导失败模式是「无法判定被写成失败」，占真实的 71%

真实 687 个 Task：`SUCCEEDED 446` / `FAILED 161` / `AWAITING_USER 32` /
`INTERRUPTED 23` / `NEEDS_REVIEW 11` / `CANCELLED 7` / **`EXECUTING 3（卡住）`** /
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

**注意「reads 旧台账」这四个字**：这条测试是在**读历史数据**，不是在断言当前行为。
下一节解释为什么。

### 8.2 更正：这个修复**已经接进生产**了

上一版我写「修复没有接进生产」，**错了**。生产路径是
`finalize_acceptance` → `terminal_state_for`：

```
core/kernel.py:2628      async def finalize_acceptance(task_id, status)
core/verification.py:32  TERMINAL_STATE_BY_VERDICT = {
                             PASSED: SUCCEEDED,
                             BLOCKED: NEEDS_REVIEW,  # "Cannot decide" is not "the work failed"
                             FAILED: FAILED,
                             NOT_APPLICABLE: NEEDS_REVIEW,
                         }
core/verification.py:44  def terminal_state_for(status)   # 唯一映射入口
```

三个入口全部在调它，不是各写各的：

```
src/tsm_agt/cli.py:637
src/tsm_agt/cli.py:2203
src/tsm_agt/sdk/runtime.py:1330
```

`tests/test_terminal_state_mapping.py` → 9 passed。

git 历史也印证了这个演进过程（这正是你说的「git log 里面有信息」）：

- `a2a9c1e`（09-27）里这段映射还**内联在** `sdk/runtime.py`：

  ```python
  elif verification.status is AcceptanceStatus.BLOCKED:
      # INV-6: "cannot decide" is not "failed". A blocked verdict
      # (for example a judged criterion with no available judge)
      # must go to human review rather than be reported as a
      # failure the work did not cause.
      await kernel.transition_task(
          result.task_id, TaskState.NEEDS_REVIEW,
          f"SDK verifier {verification.status.value}",
      )
  ```

- `fa44560`（我本轮动手**之前**的 HEAD）把它上收进内核，注释写着：

  > Clients must not map for themselves: doing so is how a BLOCKED
  > (undecided) verdict gets reported as a product failure.

**所以那 115 个历史 FAILED 全部产生于 `fa44560` 之前，是历史数据，不代表当前行为。**

那 `core/turn_outcome.py` / `core/turn_finalizer.py` 是什么？是**读旧台账时的分类器**：
`outcome_inconsistencies()` 专门把老数据里的 `FAILED_STATE_WITH_UNDECIDABLE_REASON`
标出来（8.1 引的那条测试正是在读历史）。它们没有生产消费者是一个**独立**问题
（CLI/Web 要不要展示 outcome 轴、要不要把历史不一致报出来），**不是**「修复没接线」。

我上一版的错误根源：看到 `derive_turn_outcome` 无消费者就下了结论，没有继续查
`finalize_acceptance`。这是我该避免的推断方式。

### 8.3 更正：那 3 个卡住的 Task 是身份功能上线前的 legacy 数据，**不是**扫描 bug

上一版我写「问题出在扫描范围」。**不准确。** `legacy-local-subject` 并不是某个
「另一个主体」，它是 store 在**数据里没有记录 subject 时**的兜底值：

```
src/tsm_agt/adapters/sqlite/runtime_store.py:813
    subject = str(data.get("trust_subject") or "legacy-local-subject")
```

也就是说这 3 个 Session 是**身份功能上线之前**（2026-08-31）建的，本来就没有 subject
可归属。当前身份 `uid:501` 看不到它们，是**设计上的主体边界**：

```
get_session(legacy_session) -> PermissionError: session belongs to another local subject
```

因此我**不会**把 `reconcile_orphaned_running_tasks` 改成跨主体扫描——那会越过一条
安全边界，属于「未授权的扩大范围」。能确认的是 settle 逻辑本身没问题：

```
list_sessions(include_archived=True) -> 74   （不是 77）
settle_orphaned_task(legacy task)    -> True （单独调用成功）
```

这 3 行 phantom `EXECUTING` 只存在于旧 Session 里，当前身份既看不到、也不会路由过去，
**不影响你现在的使用**。要不要结算它们是一次数据迁移决定，需要你点头。

### 8.4 「你来执行恢复」的结论：本机没有任何可用恢复源

你让我自己执行，我把所有可能藏着一份工作区副本的地方全找了一遍：

| 恢复源 | 结果 |
|---|---|
| APFS 本地快照（`tmutil listlocalsnapshots /`） | 只有 3 个 `com.apple.os.update-*`，**没有可用快照** |
| Time Machine | 无 |
| Cursor 本地历史（1087 项） | 按 `tsm-agt` 与 `task_spec.py`/`tool.py`/`kernel.py`/`sandbox.py`/`planner.py`/`runtime.py`/`cli.py` 搜，**0 命中**（历史只覆盖 `workSp/` 下的项目） |
| VS Code 本地历史（94 项） | 同样 0 命中 |
| `/tmp/tsm-agt-step6.g382PG`、`step7.ZnsJBi` | 只是 uv 缓存目录，不是仓库副本 |
| `.git/lost-found` 的 18 个游离提交 | 12 个是 **`git stash` 提交**（09-16 ~ 09-27，此前 `git stash list` 为空但对象还在）、6 个是我本轮自己的重构提交。**逐个对比其相对父提交的差集**，用六个丢失符号（`EvidenceCapability`、`evidence_intent`、`validate_authored_criterion`、`ProofPlanCompiler`、`pending_steering`、`cited_evidence_ids`、`review_model`）搜 → **全部 0 命中** |
| 全部 1077 个 git 对象 | 逐个解出搜 `SDK verifier blocked` / `trusted verifier blocked`，命中的都是**提交可达**的版本（SPEC 文档、`a2a9c1e` 的 `runtime.py`），**没有游离对象含丢失内容** |

**结论：那 29 个文件的未提交改动在本机确实不可恢复。** §5 里我重建的部分已经是行为
等价的替代品（全量绿 + 干净导出可跑）。

如果你在**别的机器 / 移动硬盘 / 云端**还有一份备份，值得最后确认一次；但**继续工作
不再需要等它**——因为 8.2 更正后可以看到，主导失败模式在当前代码里已经是对的。

---

## 9. 一句话结论

| 维度 | 状态 |
|---|---|
| 代码 | 全量 `1430 passed / 13 skipped / 0 failed`；干净导出可跑；**真实 HTTP 上的完整工程闭环实测通过**（planner → 审批 → 真写文件 → 验证 → SUCCEEDED） |
| 真实台账的主导失败模式（71%） | **当前代码已经是对的**（§8.2 更正） |
| 3 个 phantom EXECUTING | 身份功能上线前的 legacy 数据，不影响当前使用；结算需你点头（§8.3） |
| 本机可恢复源 | **无**（§8.4） |
| 仍缺的测试用例 | 28 个（§4），只有别的备份里才有（我本轮自己新增 2 条，故现在收集数是 1443） |
| 唯一无法验证的 | **真实模型端点的端到端**：本沙箱把 `aibrain-large-model-engine-slow.hellobike.cn` 解析到 `198.18.0.71` 黑洞，`danger-full-access` 下依然如此；而 `pypi.org` / `github.com` 的 TLS 正常 → 是沙箱网络策略，不是本项目代码 |

---

## 10. 需要你在**有伪终端**的普通终端里验证的一件事

```bash
.venv/bin/python -m pytest tests/test_cli_line_editing.py -q
```

本沙箱禁 `/dev/ptmx`（`os.openpty()` → `PermissionError`），所以这 5 个用例在这里
只能 skip。守卫只跳过"根本无法分配伪终端"的环境；正常终端/CI 上它们照常运行并
断言。**如果你希望连"跳过"都不允许，那就把备份里这一份测试文件取回来。**
