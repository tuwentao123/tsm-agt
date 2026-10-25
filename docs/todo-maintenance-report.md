# Todo 维护与状态可见性调整

2026-10-24。本轮实施原方案第 2、3 项；未修改工具进展 UI，也未开启 CompletionReview。

## 工具说明与维护时机

- `src/tsm_agt/adapters/builtin/todo_tools.py`：开始时标记进行中，满足完成条件和适当验证后立即更新；一次写入可完成上一项并启动下一项。仅在清单变化时写入。
- 模型可直接使用最新执行上下文或 memory/todo 结果中的 revision；缺少状态、版本冲突时才读取并核对。后端版本检查、幂等和最多一个进行中条目的限制保留。
- `merge=true` 的 schema 与现有后端能力对齐：更新已有条目只需 id 和变更字段；新增或整体替换仍由后端校验完整字段。禁止把截断后的预览当作完整清单覆盖。
- `src/tsm_agt/core/prompt_templates/coordination.md`：同步规则，增加“完成 A → 一次更新 A/B → 继续 B”的示例，保留只分析、用户改范围等边界。提示词段版本由 4.0 升为 4.1。
- 新 prompt manifest：`10f1d44392a06d336c2ce08b9f635c4d2cee9899daf0c9752d5ef44f69d12b03`。

## 最新执行状态

`src/tsm_agt/core/conversation_loop/execution_context.py` 的投影 schema 升为 2。

`plan_record` 增加当前条目、下一待办、各状态数量，以及 `maintenance_this_turn`：

```json
{
  "source": "current_turn_execution_ledger",
  "write_attempt_count": 2,
  "successful_write_count": 1,
  "failed_write_count": 1,
  "updated_this_turn": true,
  "last_successful_write_call_id": "example-update",
  "last_error_code": "INVALID_PARAM"
}
```

以上只是字段示例，不是本次真实模型评测结果。状态来自当前 turn 的执行账本，不依赖尚未被压缩的工具消息。读取不计为写入；成功但清单未变化的写入不计为进度变化。Todo 和 Working Memory 工具均记录实际清单变化；仅更新其他笔记不计为清单推进。旧记录缺少变化信息时使用 null 表示未知。前一轮的更新不计入本轮。

完整清单仍由 Working Memory 保存。投影维持 3200 UTF-8 字节限制，优先展示进行中和待办，显式记录省略数量。不会自动完成条目，不判断用户授权，不新增结束拦截。

## 验证结果

- 全量回归：1525 passed，14 skipped，5 warnings，82.20 秒。首次沙箱执行有 6 项因禁止绑定本地端口失败；另有一项会话测试出现一次时序失败。允许 loopback 测试端口后，全量复核通过。
- 最终相关测试：62 passed，包括最后补充的超长条目标识预算检查。
- `git diff --check` 与评测脚本编译检查通过。
- 已确认无执行中任务并重启本地 Web 服务；独立会话 PID 18146，HTTP 健康检查 200，新提示词与工具说明随新进程加载。
- 覆盖实际变更与无效写入、读操作、过期 revision、备注更新、跨轮恢复、压缩后账本重建、历史记录未知状态，以及评测器不能将失败、运行中或修改前的检查当作验证成功。

## 真实模型评测：当前受连接阻塞

新增 `scripts/evaluate_todo_maintenance.py`，使用项目配置的真实模型与真实 Kernel、文件工具和命令工具；工作区均为临时目录。四个场景分别是首次多步实施、承接未完成清单、只分析不修改、有修改范围限制时继续独立工作。

记录模型原始决策、清单写入和读取、完成标记时机、修改与检查顺序、最终文件和独立测试结果。代码检查与文档修改区分处理：修改文档不会强制重跑未变化代码的运行测试。对没有范围限制的实施场景，遗留 blocked 条目也不能算全部完成。

对照恢复保存的旧工具说明/schema 和旧 Planning 提示词，并去掉新增状态字段；旧 prompt manifest 必须与快照一致。CompletionReview 在两组均关闭。可执行：

```sh
.venv/bin/python scripts/evaluate_todo_maintenance.py \
  --env-file .env \
  --report-dir /tmp/tsm-todo-live \
  --baseline-snapshot docs/evaluations/todo-maintenance/baseline.json \
  --repeats 3
```

本次已尝试真实模型 `OpenAI-GPT-5.1`（reasoning_effort=low）的承接场景对照，以及只分析场景。但当前执行环境到已配置服务的 DNS、TCP 正常，TLS 握手报 `SSLEOFError / UNEXPECTED_EOF_WHILE_READING`。模型调用归类为 `connection_failed / TRANSIENT_PROVIDER`，未收到模型响应。

因此不能报告真实模型行为通过率，也不能据此证明减少了提前结束。连通后需运行上述四场景对照；评测脚本遇到连接失败会标记 `valid_run=false`，不会把它算作行为成功。

- 对照基线：`docs/evaluations/todo-maintenance/baseline.json`
- 实际失败运行：`docs/evaluations/todo-maintenance/transport-failure.json`
- 独立连接检查：`docs/evaluations/todo-maintenance/connection-diagnostic.json`

报告仅保留构造任务与测试文件，不记录模型凭据、请求头或异常响应原文。
