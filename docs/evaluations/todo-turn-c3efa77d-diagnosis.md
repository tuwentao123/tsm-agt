# Todo 未更新诊断：turn-c3efa77d21be4dcf99e55d82aaa60b96

2026-10-24。只读检查运行账本和会话记录，未修改业务会话、Todo 状态或结束策略。

## 可确认事实

- Task：`task-097b6d819d9640ae9caab17433b0efd1`。
- 用户本轮输入为“继续”；相关历史包含“按照任务清单顺序继续啊”以及同一 MCP 集成工作。
- 本轮记录序列 7687–7878，共 3 次模型回复、7 次工具调用：1 次 `core.list_files`、5 次 `core.read_file`、1 次 `core.apply_patch_v4a`。
- patch 成功创建 `src/tsm_agt/docs/mcp_integration.md`。没有 `core.todo_list` 或包含 plan 的 `core.working_memory_update` 调用，也没有本轮 `working_memory.updated` 事件。
- 最新计划仍来自事件 6824，revision 7：第 1–5 项 COMPLETED，第 6 项 `mcp-6-permissions-and-ui-labels` IN_PROGRESS，第 7 项 `mcp-7-docs-and-examples` PENDING。
- 模型最终直接汇报新增文档，并将检查留为“验证建议”，随后给出可继续补 README 链接的提议。
- 事件 7872 的结束决定为 `ACCEPT / runtime_state_terminal`。该原因表示运行状态允许结束，不表示所有 Todo 或业务验收条件已完成。
- 事件 7875 的 prompt manifest 为 `3e6af9d468e4d985409f9d8ddbbaa09bffe5346d3f76d6c69cfa2aea33249484`，与本次检查的提示词一致；工具列表包含 `core.todo_list`。不是旧版本提示词或工具未暴露。

## 上下文可见性检查

使用本轮不可变工具账本、该轮开始前的 revision 7 快照和对应工具 effect/authority，
通过当前 `project_execution_context` 重建输入尾部。最终重建文本 2751 字符、3131 UTF-8 bytes；
实际 `llm.completed.prompt_message_structure` 的最后一条也为 execution-context、2751 字符。

重建结果保留：

```json
{
  "revision": 7,
  "open_count": 2,
  "active_item_id": "mcp-6-permissions-and-ui-labels",
  "next_pending_item_id": "mcp-7-docs-and-examples",
  "maintenance_this_turn": {
    "write_attempt_count": 0,
    "successful_write_count": 0,
    "updated_this_turn": false
  }
}
```

当前项和待办项的说明、完成条件也都保留；只省略了一条已完成项。原始供应商请求正文没有持久化，
这里是依据相同版本代码及账本的确定性重建与实际结构长度交叉核对，不冒充抓取了原始请求。
现有证据不支持“压缩丢了未完成清单”。

## 机制与局限

直接原因是模型没有生成维护调用，后端按现有结束契约接受了最终文本；不是更新调用失败或前端漏渲染。

- `execution_context.py` 暴露计划、revision 与本轮维护事实，不推断条目完成或自动写入状态。
- `prompt.py::latest_tool_result_instruction` 解释失败和修改后检查状态，目前没有解释计划状态与维护时机的动态提示。
- `coordination.md` 已有“开始、满足完成条件后立即维护”的静态指令，但这仍是模型行为指令。
- `todo_tools.py::_TODO_DESCRIPTION` 的“简单任务不用 Todo”未区分新建与维护已有清单，属于可消除的歧义；不能仅凭本轮日志证明模型内部因它而决定跳过调用。
- `finalization.py` 的 `runtime_state_terminal` 不校验 Todo 与实际工作是否一致。CompletionReview 仍关闭；运行结束与业务全部完成没有被混为一个强制条件。

因此，增加可见性已生效，模型及时维护的行为仍未可靠实现。若继续调整，应区分“是否需要新建清单”
与“已有相关清单的状态同步”，明确恢复后先核对、工作边界及时更新，并通过真实模型多场景评测检查。
不能由运行时仅凭 patch 成功自动勾选，也不能把“本轮没有写 Todo”一律判成必须继续执行：
只分析、无状态变化、暂停、审批等待及真实阻塞都需要保留正常结束能力。
