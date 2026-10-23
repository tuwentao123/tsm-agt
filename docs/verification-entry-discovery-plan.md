# 验证入口发现增强方案（待 Review）

## 目标与范围

在现有 onboarding 中提供带来源、工作目录和执行参数的验证候选入口，帮助模型选择正确检查。发现不执行命令，不代表环境可用、检查通过或覆盖了本次需求。第 1、2 点的提示词调整已实施；本方案尚未实施。

本阶段不新增 Stop Gate，不自动安装依赖，不另建项目扫描系统，也不调整现有扫描生命周期和缓存策略。

## 1. 触发与注入

沿用任务进入 `RESOLVING_PROJECT` 时的 `run_project_onboarding()`，以及现有显式重扫入口。命令发现由确定性解析器完成，不调用模型。

沿用当前模型请求中的 onboarding 上下文注入。静态快照失效时保持失效语义，不把旧入口描述为当前事实；自动增量刷新属于后续第 4 点，不混入本次改造。

## 2. 数据模型与兼容

给 `OnboardingFact` 增加可选的结构化详情字段，缺省为空；历史快照仍能反序列化。命令事实继续保留可读的 `value` 和现有来源列表。新事实的 identity/hash 包含详情，避免相同命令在不同子目录互相覆盖。

示例：

```json
{
  "category": "observed_command",
  "value": "pnpm run test",
  "details": {
    "cwd": "packages/web",
    "argv": ["pnpm", "run", "test"],
    "kind": "test",
    "discovery_basis": "manifest_script_and_package_manager",
    "availability": "not_checked",
    "execution_status": "not_run"
  },
  "sources": [
    {"path": "packages/web/package.json", "sha256": "..."},
    {"path": "pnpm-workspace.yaml", "sha256": "..."},
    {"path": "pnpm-lock.yaml", "sha256": "..."}
  ]
}
```

候选入口按 `(cwd, argv)` 去重，不再按命令字符串去重。`cwd` 始终相对当前工作区；结果不越过工作区或现有路径授权边界。

## 3. 命令解析

- Node：只输出 manifest 中实际存在的检查脚本；包管理器优先使用有效的 `packageManager` 声明，其次使用明确的所属 workspace 配置和锁文件证据。独立嵌套项目保留自己的边界，不能直接继承父目录任意锁文件。
- 多个锁文件冲突、声明与锁文件冲突，或者无法确定所属 workspace：返回来源和歧义，交由模型进一步读取配置；不静默选择某一个工具。没有包管理器证据时只报告脚本存在，执行方式保持未解析，不把 npm 回退当作确定事实。
- Node 的 `test`、`lint`、`typecheck`、`check`、`build` 保留不同检查类型；`format` 不作为只读验证候选自动推荐。自定义名称不只凭名称猜测含义，仍可由模型读取配置补充发现。
- Python：保留 pytest/ruff 的配置来源及所在项目目录；保留已有本地 Python 可执行入口供模型选择，不把存在解释器等同于已安装依赖。
- Gradle、Cargo、Go、Maven：保留已有发现规则，补充项目目录和参数；语言惯例生成的候选需标注推断依据，不声称配置中显式定义了该命令。
- 继续使用有界文件读取及现有路径解析；配置损坏、文件过大或内容类型异常时不生成虚构入口。对相关配置的解析问题输出简短诊断，供模型决定是否进一步读取。

## 4. 模型可见内容

注入内容分别呈现：已观察的脚本定义、推断的执行入口、歧义和未检查项。要求模型根据本次修改选择检查范围，不能机械运行所有入口。

例如：在 `packages/web` 发现 `test` 脚本，包管理器依据为 pnpm workspace 与锁文件；候选命令 `pnpm run test`，尚未执行，依赖可用性未知。

发现结果仍是项目数据，不升级为可信指令；命令执行继续走现有工具与权限流程。

## 5. 涉及代码

- `src/tsm_agt/core/onboarding.py`：可选详情序列化、按项目目录解析、候选去重、冲突诊断和来源。
- `src/tsm_agt/core/kernel_onboarding.py`：模型上下文投影详情和歧义，保持不可信数据边界。
- `src/tsm_agt/core/kernel_task.py`：核对现有 onboarding 事实投影是否需要透传新详情，避免另一模型上下文遗漏目录。
- `tests/test_project_onboarding.py` 及相关上下文测试：验证解析、快照兼容与实际注入。

## 6. 验收

1. 根项目和两个子项目均有 `test` 时，三个入口全部保留且执行目录正确。
2. 独立 pnpm/yarn/npm 项目及明确的 monorepo 均得到来源可追踪的候选。
3. 包管理器证据冲突时保留歧义，不生成一个伪确定入口。
4. manifest 没有测试脚本时，不虚构测试命令。
5. Python、Gradle、Cargo、Go、Maven 的现有发现不回退。
6. 损坏配置、大文件、路径越界和扫描截断不会被描述为完整发现。
7. 旧快照仍可读取；新快照详情往返一致，过期事实不继续当作有效入口注入。
8. 通过模型请求边界测试确认 `cwd`、`argv`、来源和 `not_run` 实际进入上下文。
9. 静态发现测试证明未启动任何项目命令、未安装依赖。

## Review 决策

建议本阶段先支持明确入口与明确歧义；自定义测试脚本的语义识别、自动增量刷新及用户自定义覆盖配置分别留在后续，不用猜测扩大范围。
