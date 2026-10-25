# Todo 已关闭导致后续工具被取消

## 本轮事实

排查对象：`turn-70e360cefa6d4ea1878eb62b4a9925ad`。
Task 为 `task-097b6d819d9640ae9caab17433b0efd1`。

- 本轮用户要求“设计完整吧 设计完成 直接开始实施”。
- 启动时继承 revision 8 的旧清单，七项状态都是 `COMPLETED`。
- 模型第一轮实际请求了四个 `core.read_file`，准备读取 MCP client、provider、composition 和集成文档。
- 事件 8192：`plan.action_evaluated` 将 `should_stop` 设为 true，`step_id=plan-finished`，无进展次数为 0。
- 事件 8193：停止策略返回 `SUMMARIZE_WITH_EVIDENCE`，`reason=plan_finished`，`terminal=true`。
- 事件 8197–8200：四个已接收但未开始的调用均记为 `CANCELLED_BY_RUNTIME_POLICY`。
- Runtime 随后注入 `Do not call tools. Answer from existing evidence...`；模型第二轮按该指令输出方案。本轮实际工具执行数为 0。

因此，这是旧清单状态误触发的运行时拦截。没有触发 CompletionReview，没有耗尽模型/工具预算，也没有实际发生连续无进展。
调用被标为终止类取消并要求直接总结，因而模型没有自行重试。

上一次维护优化只覆盖提示词、状态投影和提醒，没有覆盖这条既有硬拦截，是遗漏。

## 修复

| 文件 | 改动 |
| --- | --- |
| `src/tsm_agt/core/plan_guard.py` | 不再将 `plan_finished` 合并到 `should_stop`；清单关闭仅作状态观察，停止仍由真实重复无进展计数决定；移除“不要开始新工具工作”的旧说明。 |
| `src/tsm_agt/adapters/rule_based_stop_or_pivot/policy.py` | 删除看到 `plan_finished` 就终止的分支；清单关闭不能覆盖真实预算与无进展判断。 |
| `src/tsm_agt/core/kernel_policy.py` | 无策略适配器的回退路径同样不把关闭清单当成完成结论；实际无进展仍返回停止。 |
| `src/tsm_agt/ports/stop_or_pivot_policy.py` | 明确 `plan_finished` 是兼容的观察字段，可能包括全阻塞清单，不是终止信号。 |

同一轮清单完成后可以继续检查；下一轮新要求可以读文件、重新打开旧项或更新清单；全阻塞清单也允许检查新证据和恢复工作。
修复不主动重开或修改用户清单，不自动重放已经结束的真实输入。审批、明确拒绝、工具数量预算和真实无进展防护保持有效。

## 验证

使用新增回归测试先在旧代码运行，四个测试失败，复现第一批读取被取消以及 SQLite 重开后下一轮无法继续。
修复后相关 77 项测试通过，包括同轮四个真实文件读取、SQLite 恢复后读取并重开 completed/blocked 事项、策略有无两条路径，以及真实重复与预算仍可停止。

使用真实历史清单与四个原始调用参数作只读判定重放，四个调用修改前均 `should_stop=true`，修改后均为 false，无进展计数均为 0。
该判定重放不执行用户任务，也不修改真实数据库。记录见 [重放证据](evaluations/plan-finished-stop/turn-replay.json)。

完整回归：1574 passed、14 skipped、2 failed、5 warnings，93.26 秒。两项失败分别是：

- 既有的 `mcp_demo_fixture.py -> mcp_client_manager` 跨适配器导入架构检查，未在本轮改动。
- `test_a_new_message_restarts_an_interrupted_conversation` 在完整回归中观察到两轮而非三轮；单独重跑通过。为判断是否回归，在上一次提示词优化前的完整源码快照上独立重复二十次，出现一次同样的 `2 != 3`；当前修复源码二十次未复现。该结果支持其为既有的间歇问题，不等于该问题已经修复。记录见 [会话恢复重复检查](evaluations/plan-finished-stop/conversation-recovery-repeat.json)。

本地服务启用结果见 [部署记录](evaluations/plan-finished-stop/deployment.json)。本次只重启开发服务，不重放这轮已结束的输入，不更改其历史清单。
