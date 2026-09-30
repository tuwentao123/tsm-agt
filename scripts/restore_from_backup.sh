#!/usr/bin/env bash
# 从备份恢复 `git reset --hard` 事故丢失的未提交改动。
#
# 设计原则：
#   1. 默认**只预览**，不写任何文件。要真恢复必须显式加 --apply。
#   2. 只碰清单里的 29 个文件；明确排除 3 个「勿覆盖」文件。
#   3. 先检查备份根目录确实像这个项目，避免相对路径拼错后到处乱拷。
#   4. 每个文件都打印「当前 HEAD 版本 vs 备份版本」的差异行数，让你能看出
#      备份是不是比当前更旧（旧备份覆盖会把已修好的东西退回去）。
#
# 用法：
#   scripts/restore_from_backup.sh <备份根目录>              # 预览
#   scripts/restore_from_backup.sh <备份根目录> --apply      # 真正恢复
#
# 回退：本项目所有改动都已提交，任何被覆盖的文件都能用
#   git checkout HEAD -- <路径>
# 原样拿回来。

set -euo pipefail

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

[ $# -ge 1 ] || usage
BACKUP_ROOT="${1%/}"
APPLY=0
[ "${2:-}" = "--apply" ] && APPLY=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------- 前置检查
[ -d "$BACKUP_ROOT" ] || { echo "错误：备份目录不存在：$BACKUP_ROOT" >&2; exit 1; }
for probe in src/tsm_agt/core/kernel.py src/tsm_agt/sdk/runtime.py; do
    if [ ! -f "$BACKUP_ROOT/$probe" ]; then
        echo "错误：$BACKUP_ROOT 里找不到 $probe" >&2
        echo "      这个目录看起来不是 tsm-agt 的仓库快照。请指向含 src/ 的那一层。" >&2
        exit 1
    fi
done

# 已跟踪文件必须是干净的，否则恢复后分不清哪些差异是备份带来的。
# 未跟踪文件不影响这次操作（清单里 29 个文件全是已跟踪的），所以不拦。
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "错误：工作区有未提交的已跟踪改动。先提交或 stash，再恢复。" >&2
    git status --short --untracked-files=no >&2
    exit 1
fi

if [ "$APPLY" = "1" ]; then
    MARKER="$(git rev-parse --short HEAD)"
    echo "将在恢复前创建可回退标签：pre-restore-$MARKER"
    git tag -f "pre-restore-$MARKER" >/dev/null
    echo "回退命令：git checkout pre-restore-$MARKER -- <路径>"
    echo
fi

# ------------------------------------------------------- 丢失清单（29 个）
LOST=(
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
)

# ------------------------------- 勿覆盖：既有改动已在提交里，覆盖会回退
DONT_TOUCH=(
    src/tsm_agt/bootstrap/composition.py
    tests/architecture/test_dependencies.py
    tests/test_completion_stall_terminal.py
)

# ---------------------------------------------------------------- 预览/恢复
restore_one() {
    local rel="$1"
    local src="$BACKUP_ROOT/$rel"
    local dst="$REPO_ROOT/$rel"

    if [ ! -f "$src" ]; then
        printf '  %-52s 备份里没有，跳过\n' "$rel"
        return
    fi
    if [ ! -f "$dst" ]; then
        printf '  %-52s 当前不存在，将新增\n' "$rel"
    elif cmp -s "$src" "$dst"; then
        printf '  %-52s 相同，无需恢复\n' "$rel"
        return
    else
        local n
        n="$(diff -u "$dst" "$src" | grep -c '^[+-][^+-]' || true)"
        printf '  %-52s 有差异（%s 行）\n' "$rel" "$n"
    fi

    if [ "$APPLY" = "1" ]; then
        mkdir -p "$(dirname "$dst")"
        cp "$src" "$dst"
    fi
}

echo "备份根目录：$BACKUP_ROOT"
echo "仓库根目录：$REPO_ROOT"
echo
echo "==== 将要恢复的 29 个文件 ===="
for rel in "${LOST[@]}"; do restore_one "$rel"; done

echo
echo "==== 明确不碰的 3 个文件（覆盖它们会回退已提交的改动）===="
for rel in "${DONT_TOUCH[@]}"; do
    printf '  %-52s 跳过\n' "$rel"
done

echo
if [ "$APPLY" = "1" ]; then
    cat <<'EOF'
==== 已恢复。接下来按顺序做验收 ====

1) 收集到的用例数应当回到 ~1470（事故前 1469 + 本轮新增 1）
   .venv/bin/python -m pytest tests/ --collect-only -q | tail -1

2) 全量测试
   .venv/bin/python -m pytest tests/ -q

3) 主导失败模式的产码应当出现了（此前只有 turn_outcome.py 的识别列表）
   grep -rn "verifier blocked" src/ --include=*.py

4) 事件序列应从 VERIFYING -> FAILED 改成 -> NEEDS_REVIEW
   （在副本上验证，别动真库）

5) 3 个卡在 EXECUTING 的 Task 应能被结算（需扫描范围也修好）
   .venv/bin/python -c "
import sqlite3, json
con = sqlite3.connect('file:.agent/runtime.db?mode=ro', uri=True)
n = sum(1 for (dj,) in con.execute('select data_json from runtime_tasks')
        if json.loads(dj).get('state') == 'EXECUTING')
print('EXECUTING:', n, '(期望最终为 0)')"

任何新失败，把 `pytest` 的失败列表发我，我按失败逐个定位，只做证据驱动的判断。
EOF
else
    cat <<'EOF'
==== 这是预览，没有修改任何文件 ====
确认上面的差异方向正确（备份不应比当前更旧）后，重跑并加 --apply：

  scripts/restore_from_backup.sh <备份根目录> --apply

如果某个文件备份版本比当前更旧、你不想用它，可以先只恢复其余文件，
再单独用 `git checkout HEAD -- <路径>` 把那个文件退回当前版本。
EOF
fi
