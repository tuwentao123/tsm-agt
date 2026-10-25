# 文件读取结果复用：实施与验证

日期：2026-10-24。已完成首版实现。未重启现有 Web 进程；当前数据库有待审批的活动 checkpoint，保留该进程以免打断现有恢复流程。服务重启后新进程加载此实现。

## 实际行为

每次 `core.read_file` 仍检查路径和权限、实际读取文件、计算整文件 SHA-256。
执行账本和 checkpoint 中保留完整结果。模型输入层才折叠同一 Task、读取根、规范路径、
文件版本、请求/实际范围、分页游标及可见正文均匹配的副本。

第一次仍存续的结果保留正文；后续结果保留独立 call_id、哈希、来源和分页信息，正文替换为
`status=unchanged`、`content_returned=false` 和 `reused_from_call_id`。短文本若替换后不省空间则保留。
脱敏、失败、缺乏可靠来源的结果不折叠。不同路径、版本和范围不合并。

例如三次相同读取原来都是完整正文；现在模型看到 `a: 完整正文`、`b: 引用 a`、`c: 引用 a`。
压缩删除 a 后，依据剩余规范消息重新生成 `b: 完整正文`、`c: 引用 b`，并按这个布局重新估算预算。
不在预算通过后补回正文。只有摘要或失效旧引用时，不声称模型已经看过正文，需要正常重新读取。

## 改动位置

| 文件 | 实施内容 |
| --- | --- |
| `src/tsm_agt/core/read_result_reuse.py` | 无状态纯投影、严格匹配、可见来源选择、旧引用校验及字节统计。 |
| `src/tsm_agt/core/context.py` | 规范消息与模型消息分开；所有压缩候选及最终预算使用相同投影。 |
| `src/tsm_agt/core/conversation_loop/loop.py` | 启用读取投影；压缩后的 checkpoint 保存规范消息；实际请求使用投影。记录 `context.read_results_reused` 诊断事件，不存正文。 |
| `src/tsm_agt/adapters/builtin/core_tools.py` | 工具说明增加引用语义；没有增加磁盘读取缓存或重复调用硬拦截。 |
| `src/tsm_agt/core/prompt_templates/workspace.md`、`src/tsm_agt/core/prompt.py` | 读取规则说明引用、缺失正文、不同版本与 expected_hash，规则版本 1.1。 |
| `tests/test_read_result_reuse.py` | 30 个测试用例，包含生产读取器、完整会话循环、SQLite 重开及三种模型请求协议。 |
| `scripts/evaluate_read_result_reuse.py` | 构造文件的前后输入测量；可选真实模型引用理解烟测。 |

没有加入磁盘正文缓存、并行执行器、通用输出缓存或 CompletionReview。
历史 session 文本中的内嵌读文件摘要，以及搜索、命令、MCP、Skill、多媒体结果不走这次折叠规则。

## 验证结果

- 相关回归：151 passed。
- 全量回归：1556 passed、14 skipped、5 warnings，86.50 秒。警告为既有 FastAPI 生命周期及 multipart 弃用提示。
- 文件外部修改、原子替换、相同长度且保留 mtime 的修改、窗口外修改：重新实际读取后 SHA 改变，保留新正文。
- 文件删除、链接越界、二进制或非 UTF-8 替换：现有检查正常失败，不因先前读取成功而放行。
- 超长单行按列分页：引用保留 `truncated`、`next_cursor` 与 `next_offset`，后续不同列范围返回自己的正文。
- 压缩删除旧来源：新来源在实际请求中存在，checkpoint 仍保留剩余完整结果；SQLite 重开后的工具执行记录也保持完整。
- OpenAI Chat、Responses、Anthropic 请求均保留完整来源正文和独立调用对应关系；Responses reasoning 块保留。

构造样例实测（详情含原始/投影视图，见 [JSON](evaluations/read-result-reuse-20261024.json)）：

| 指标 | 折叠前 | 折叠后 |
| --- | ---: | ---: |
| 三次同版本同范围读取的工具结果 JSON，UTF-8 bytes | 13,893 | 7,449 |
| 同一请求中的完整正文份数 | 3 | 1 |
| 请求估算输入 token，包含相同当前提示词及工具定义 | 6,158 | 4,546 |

工具结果减少 6,444 bytes。估算采用既有 `utf8-bytes-div-4-conservative-v1`，不是供应商计费数据。
这个例子包含三次相同读取与一次修改后读取，实际共执行四次读取；不减少文件 I/O。

真实配置模型 `OpenAI-GPT-5.1` 成功从上述投影请求回答 `READ_REUSE_OK`。
这是一次真实模型理解引用的烟测：读取调用由评测程序构造并通过真实工具执行，
不是模型自主生成整个工具流程，也没有证明修改验证、Todo 维护或复杂任务完成率改善。
此前截图轮次的九次读取没有完全相同的正文组，多为不同文件或不同范围，本优化不会消除那些必要读取。

复现命令：

```bash
.venv/bin/python -m pytest -q tests/test_read_result_reuse.py
.venv/bin/python scripts/evaluate_read_result_reuse.py --report /tmp/read-reuse.json
.venv/bin/python scripts/evaluate_read_result_reuse.py --report /tmp/read-reuse-live.json --live
```

这次没有提交或推送 Git 变更。
