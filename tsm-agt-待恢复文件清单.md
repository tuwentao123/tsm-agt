# 待恢复文件清单（`git reset --hard` 事故）

> 事故：为修改三个提交信息里写错的行数，我执行了 `git reset --hard`。该命令会丢弃
> **工作区未提交的改动**，而这个仓库在本次会话开始前有 32 个跟踪文件带着未提交改动。
> 本文是恢复用的清单与验收步骤。
>
> 恢复源：你自己的备份 / 编辑器本地历史。git 内的恢复途径已穷尽（无 stash、无悬空
> blob/tree/commit 含该内容、所有分支与远程均从未提交过、`__pycache__` 已在 21:03 按
> 还原后的源码重编）。

---

## 1. 需要恢复到工作区的 29 个文件

这 29 个文件现在**是纯 HEAD 版本**（工作区相对 HEAD 干净），它们的既有改动已丢失。

```
src/tsm_agt/adapters/builtin/__init__.py
src/tsm_agt/adapters/builtin/code_intelligence_tools.py
src/tsm_agt/adapters/builtin/core_tools.py
src/tsm_agt/adapters/builtin/task_spec_tools.py
src/tsm_agt/adapters/local_sandbox/sandbox.py
src/tsm_agt/adapters/model_rubric_judge/judge.py
src/tsm_agt/adapters/model_task_spec_planner/planner.py
src/tsm_agt/adapters/posix_path/path.py
src/tsm_agt/adapters/windows_path/path.py
src/tsm_agt/bootstrap/model_configuration.py
src/tsm_agt/cli.py
src/tsm_agt/core/task_runtime_projection.py
src/tsm_agt/core/task_spec.py
src/tsm_agt/ports/rubric_judge.py
src/tsm_agt/ports/tool.py
src/tsm_agt/ports/workspace_path.py
src/tsm_agt/sdk/runtime.py
src/tsm_agt/web/app.py
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

### 不需要恢复的 3 个（既有改动已在提交里，勿覆盖）

这三个当初被我 `git add` 时把整个文件（含既有改动）提交了，所以历史里已有；
**从备份覆盖它们反而会回退我这几轮的改动**：

| 文件 | 已在哪个提交 |
|---|---|
| `src/tsm_agt/bootstrap/composition.py` | `b49e714`（及后续） |
| `tests/architecture/test_dependencies.py` | `db9e677`（棘轮表） |
| `tests/test_completion_stall_terminal.py` | `def49cb`（`TERMINAL_AUTHORITY` import 重指） |

---

## 2. 恢复前先做的一件事

**先备份当前状态，再覆盖。** 我这轮的全部改动都在提交里，但留个 patch 更安全：

```bash
cd /Users/tusima/Documents/学习/tsm-agt
git diff > /tmp/current.patch        # 现在应为空
git stash list                       # 应为空
git log --oneline -1                 # 记为 BASE，恢复出问题时可以回来
```

覆盖前建议先 diff 你的备份版本与当前版本，确认备份不是更早的快照
（编辑器本地历史有时只留最近几次保存）：

```bash
diff -u src/tsm_agt/ports/tool.py <你的备份>/src/tsm_agt/ports/tool.py | head -60
```

**只覆盖文件，不要提交。**

---

## 3. 恢复后的验收（三步）

```bash
# ① 应当恰好列出 29 个 " M"（不多不少）
git status --short | grep '^ M' | wc -l

# ② 包能导入了（当前这里是 ImportError: cannot import name 'EvidenceCapability'）
.venv/bin/python -c "import tsm_agt.ports; print('ports OK')"

# ③ 全量绿
.venv/bin/python -m pytest -q
```

**"恢复完成"的定义**：

| 检查 | 期望 |
|---|---|
| ① 修改文件数 | 29 |
| ② `import tsm_agt.ports` | 成功（3 个符号齐了：`EvidenceCapability`、`EXPLICIT_ONLY_EVIDENCE_CAPABILITIES`、`derive_evidence_capabilities`） |
| ③ 全量测试 | `1456 passed, 13 skipped, 4 warnings` |

第 ③ 条的 1456 是事故前在 `a0c844f` 上实测的数字；事故只丢工作区改动，没有改动任何提交，
所以恢复到位后应当回到这个数字。

---

## 4. 如果 ③ 不是 1456

说明备份不完整。把 `pytest` 的失败列表发我，我按失败逐个定位缺的是哪份文件的哪部分，
**只做证据驱动的判断，不猜**。

另外：被删的 28 份 SPEC 完整保存在 git 里，随时可按名取回，例如——

```bash
git show '6f2a870^:Artifact Graph与Proof Obligation任务证明闭环实施SPEC.md'
```

其 §4.3 完整写明了 `EvidenceCapability` 的 8 个成员、每个成员由
`(effect, result_authority)` 的哪个组合派生、`extra_evidence_capabilities` 字段，
以及显式声明与派生冲突时启动期 fail-closed。这是恢复 `ports/tool.py` 那部分时的
**权威依据**（若备份里恰好缺这一块）。

---

## 5. 另一件必须知道的事：HEAD 依赖 7 个未跟踪文件

**已提交的文件 import 了从未提交过的模块**，所以 HEAD 单独不可跑：

| 未跟踪模块 | 被哪些**已提交**文件 import |
|---|---|
| `core/verification_command.py` | `core/kernel.py`、`core/kernel_tool.py` |
| `core/evidence_coverage.py` | `core/kernel.py`、`core/__init__.py` |
| `core/proof_projection.py` | `core/kernel.py`、`core/__init__.py` |
| `core/proof_obligation.py` | `core/__init__.py` |
| `core/artifact_graph.py` | `core/__init__.py` |
| `core/semantic_review.py` | `core/kernel.py` |
| `core/turn_outcome.py` | 仅被 `core/turn_finalizer.py`（也未跟踪）引用 |

（`core/turn_finalizer.py` 目前只被未跟踪的 `tests/test_proof_closure.py` 引用。）

### 两个后果

1. **恢复时绝不能让这 13 个未跟踪文件被删除或覆盖。** 它们是 proof 栈的实体，且是
   HEAD 能运行的前提。用 `git clean` 之类的命令会直接把它们清掉。
   它们现在完整保存在磁盘上：

   ```
   src/tsm_agt/adapters/builtin/todo_tools.py
   src/tsm_agt/core/artifact_graph.py
   src/tsm_agt/core/evidence_coverage.py
   src/tsm_agt/core/proof_obligation.py
   src/tsm_agt/core/proof_projection.py
   src/tsm_agt/core/semantic_review.py
   src/tsm_agt/core/turn_finalizer.py
   src/tsm_agt/core/turn_outcome.py
   src/tsm_agt/core/verification_command.py
   tests/test_orphaned_running_tasks.py
   tests/test_proof_closure.py
   tests/test_task_spec_planner_prompt.py
   tests/test_todo_tools.py
   ```

2. **恢复完成、全量绿之后，建议立刻把这 13 个文件提交**（或至少复制一份到仓库外）。
   在此之前，HEAD 在任何新克隆上都无法导入——这不是本次事故造成的，但本次事故说明
   了这个状态的风险有多大。

