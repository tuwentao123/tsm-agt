# 源码比较来源证据合同实施 Spec

## 1. 背景与问题

当前 Runtime 将 `web.search`、读取第三方文章、GitHub Issue 与 GitHub 源码文件都视为成功的 `OBSERVE` 执行。完成合同只检查是否存在观察证据，无法区分“搜索候选”与“已读取、版本固定的源码制品”。因此，模型即使只拿到第三方搜索摘要，也可能将其表述为源码级结论并完成任务。

本 Spec 为“要求基于公开源码比较实现”的任务增加通用来源证据合同。它不针对 Codex、Hermes 或任何固定项目名称；判断依据是用户目标中明确同时出现比较语义与源码来源语义。

## 2. V1 范围

### 包含

- 新增可持久化的 `SourceArtifact`，表示一份已读取、固定版本的 GitHub 文件。
- 将 `SourceArtifact` 写入 `ToolFactDescriptor`，沿用既有 tool result、事件、Task snapshot 和 SQLite 持久化链路。
- 仅由 `source.github.get_file` 的成功、未截断结果创建 `SourceArtifact`。
- 在 SINGLE_LOOP 的主 completion contract 中，检测明确的源码比较任务；此类任务必须在当前 turn 中拥有来自至少两个不同 GitHub 仓库的 `SourceArtifact`。
- 缺失时产生可恢复的 `SOURCE_COMPARISON_ARTIFACTS_MISSING` gap，复用现有 continuation/incomplete-boundary 流程。

### 不包含

- 不新建 GitHub adapter。现有 `GitHubSourceToolProvider` 已负责 ref → immutable commit SHA、blob SHA、canonical URL 与内容 hash。
- 不把 `web.search`、`web.open`、第三方网页、Issue、PR 或 tree 结果提升为源码制品。
- 不自动判断任意仓库是否为某个项目的官方仓库。
- 不从任意自然语言可靠提取每一个比较对象；V1 使用“至少两个不同仓库”的保守下限。显式对象到 canonical repo 的绑定留给后续版本。
- 不改变普通问答、仅本地代码分析、单仓库阅读或旧任务的完成行为。

## 3. 数据契约

```python
@dataclass(frozen=True, slots=True)
class SourceArtifact:
    source_kind: str       # V1 固定为 "github"
    repository: str        # owner/repository
    commit_sha: str        # 40 位小写 SHA
    path: str              # 相对文件路径
    blob_sha: str          # 40 位 SHA
    canonical_url: str     # https://github.com/<repo>/blob/<sha>/<path>
    content_hash: str      # sha256:<hex>
```

`ToolFactDescriptor` 增加 `source_artifacts: tuple[SourceArtifact, ...] = ()`。该字段必须完整 `to_data/from_data`，以便它自然写入 `ToolResult`、`tool.completed` 事件与 `TaskSnapshot.tool_executions`。

一个 `SourceArtifact` 仅在以下条件同时成立时有效：

1. 工具名为 `source.github.get_file`；
2. 工具结果成功并已提交；
3. `ToolResult.truncated` 为 `False`；
4. 返回 `repo`、固定的 40 位 `ref`、非空 `path`、40 位 `blob_sha`、canonical GitHub blob URL 和 `sha256:` 内容 hash。

## 4. 源码比较识别

Runtime 只在用户 root goal 明确同时包含以下两类语义时启用 V1 合同：

- 比较语义：`compare`、`comparison`、`对比`、`比较`；
- 来源语义：`source code`、`open source`、`github`、`upstream implementation`、`源码`、`源代码`、`开源`、`上游实现`。

该识别只决定“是否需要源码制品”，不负责识别项目名称，不将模型 Planner 的 scope/constraints 作为 authority。

## 5. Completion Contract

`validate_completion()` 在现有 mutation、verification、workspace/runtime dependency 检查之后执行来源证据检查：

```text
if root goal is source comparison:
    artifacts = committed successful execution descriptors
                where source_artifact is complete and untruncated
    if distinct artifact.repository count < 2:
        add SOURCE_COMPARISON_ARTIFACTS_MISSING
            required_effects = (OBSERVE,)
            reference = source-comparison
```

`web.search` 是 discovery，不会产生 artifact，故不能关闭该 gap。`source.github.resolve`、`list_tree`、`search_code`、`get_commit`、`get_pull_request`、`get_issue` 也不能关闭该 gap，因为它们不是已读取文件。

现有 SINGLE_LOOP 行为保持不变：有模型预算和可见 `OBSERVE` tool 时，将 gap 注入 completion continuation；否则记录 `turn.incomplete_boundary`。

## 6. 状态与兼容性

- 老事件的 descriptor 不含 `source_artifacts` 时反序列化为空 tuple。
- GitHub 工具的原有 `artifact_refs`、`scope`、`content_hash` 保持不变。
- 只有新建 task、且 root goal 命中明确源码比较条件时才新增完成要求；非目标任务无行为变化。
- V1 不接受截断文件作为完整源码比较证据，避免只凭局部内容做实现级结论。

## 7. 测试

1. `SourceArtifact` 的序列化、旧 descriptor 兼容和非法字段拒绝。
2. `source.github.get_file`：mutable ref 被固定为 SHA 后构造 artifact；tree/commit 等结果不构造 artifact；截断文件不构造 artifact。
3. completion contract：
   - 命中源码比较且无 artifact → gap；
   - 只有 search 或一个仓库 artifact → gap；
   - 两个不同仓库、未截断 pinned artifact → 可完成；
   - 截断 artifact → gap；
   - 非源码比较目标 → 不新增 gap。
4. 回归 GitHub source 与 single-loop completion 定向测试。

## 8. 后续阶段

V2 在用户/Planner 明确声明 comparison targets 后，增加 `SourceRequirement(subject, canonical_repository?)`，让 gate 从“两个不同仓库”升级为“每个指定对象各有一个满足约束的 SourceArtifact”。
