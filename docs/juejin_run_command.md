# 从模型发起到结果回传，拆解 tsm-agt 的 run_command

用户说「跑一下单测，失败的话看看原因」。模型决定调用 `core.run_command`。测试跑了 90 秒，这 90 秒里谁在等？测试结束后，退出码和日志又是怎么回到模型上下文里的？

在 tsm-agt 里，**模型决定运行什么，Runtime 负责执行并记录结果，模型收到结果后再决定下一步**。当前台命令需要等到退出时，等待发生在同一次工具调用里。用户可以看到进度，模型在等待期间无需轮询。

> 源码链接指向 GitHub `main` 上的对应文件。[tsm-agt 仓库](https://github.com/tuwentao123/tsm-agt)

## 1. 模型通过工具调用提出一条命令

支持工具调用的模型会在请求中收到工具名、描述和参数 Schema。用户要求运行测试时，模型可以返回下面这样的调用。这里用 tsm-agt 内部的 `ToolCall` 格式展示。

```json
{
  "call_id": "call-test-1",
  "name": "core.run_command",
  "arguments": {
    "argv": ["python", "-m", "pytest", "-q", "tests/test_example.py"],
    "cwd": "."
  }
}
```

- `argv` 的第一项是可执行程序，后面的每一项都是独立参数。执行器直接把它们交给操作系统。管道、重定向等 Shell 语法在这里没有特殊含义。
- `cwd` 必须显式提供。`.` 表示当前任务的工作区根目录，也可以指定已定位到的子目录。
- 省略 `mode` 就走前台。测试结果决定下一步时，省略 `timeout_seconds`，让工具等待进程终态。

这条消息表达的是**执行请求**。模型适配器把各家 API 的工具调用转换成统一的 `ToolCall`，其中 `call_id` 会贯穿该请求及其结果。[工具定义](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/adapters/builtin/process_tools.py) · [工作区提示词](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/prompt_templates/workspace.md) · [ToolCall 和 ToolResult](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/ports/tool.py)

## 2. Runtime 校验、授权，再启动进程

会话循环收到工具调用后，检查工具是否在本次请求中声明、参数是否符合 Schema，并记录待执行的工具批次。轮到 `core.run_command` 时，Runtime 预检工作目录和可执行程序，并根据工作区信任级别和具体命令做权限判断。需要审批时，先保存当前轮的检查点，用户批准对应请求后，从这个检查点继续执行。通过执行沙箱检查后，进程执行器才会真正启动程序。[会话循环](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/conversation_loop/loop.py) · [工具执行入口](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/kernel_tool.py) · [权限策略](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/policy.py) · [审批恢复](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/kernel_approval.py)

Runtime 为进程分配 `process_id`，启动后保存进程记录并写入 `process.started`。执行器在进程运行时收集 stdout 和 stderr。**`call_id` 用来把结果配回模型发出的那次调用，`process_id` 用来跟踪实际进程。** 同一个进程可以先返回启动回执，随后被另一次 `process_wait` 或 `process_logs` 调用观察。这些调用各有自己的 `call_id`，共用原来的 `process_id`。[进程登记与状态](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/kernel_process.py) · [POSIX 执行器](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/adapters/local_process/executor.py) · [Windows 执行器](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/adapters/windows_process/executor.py)

## 3. 前台命令在同一次工具调用里等到退出

```text
模型请求 1  →  提出 core.run_command
Runtime     →  校验 / 授权  →  启动并登记进程
工具调用中  →  等待进程退出，网页可以显示进程已启动
进程退出    →  记录终态，收集退出码和输出
模型请求 2  →  带上原调用和对应结果，模型决定下一步
```

前台模式默认等待同一个进程进入终态。等待期间，会话循环仍停在工具调用处。通用工具的短超时不会截断这段等待。进程有自己的最长运行时间，工具提供器默认是 3,600 秒，由 Runtime 管理。用户取消前台执行时，Runtime 可以停止托管进程并记录实际终态。[前台等待与工具时限](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/kernel_tool.py) · [进程期限与终态](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/kernel_process.py)

如果调用方显式传入兼容参数 `timeout_seconds`，它只限定本次前台观察窗口，最多 10 秒。窗口结束时进程仍在运行，就返回进程回执。已经拿到 `process_id` 后，如果接下来必须使用它的退出结果，可以调用 `core.process_wait` 并传入 `until: "exit"`。只想短暂观察时，才用 `wait_seconds`。[进程工具参数](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/adapters/builtin/process_tools.py) · [参数校验](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/tool.py)

## 4. 工具结果怎样进入下一次模型请求

进程退出后，工具提供器将退出状态、退出码和输出包装成 `ToolResult`。Runtime 处理敏感内容，持久化结果并记录 `tool.completed` 或 `tool.failed`。会话循环把结果写成一条 `TOOL` 消息，保留原调用的 `call_id`。当前批次的结果收齐后，下一次模型请求会带上原来的工具调用及其结果。[结果封装](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/adapters/builtin/process_tools.py) · [结果脱敏](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/tool_result_governor.py) · [结果进入会话](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/turn_tool_batch.py)

下面是一次测试失败返回给模型的结果，省略了时间戳、分页元数据等字段。

```json
{
  "call_id": "call-test-1",
  "ok": false,
  "error_code": "PROCESS_EXIT_NON_ZERO",
  "data": {
    "process_id": "process-example",
    "mode": "foreground",
    "status": "exited",
    "exit_code": 1,
    "succeeded": false,
    "output_observed": true,
    "stdout": {"text": "1 failed"},
    "stderr": {"text": ""}
  }
}
```

模型可以根据这份结果说「测试进程退出，退出码为 1，输出显示有 1 个测试失败」。要定位具体原因，还需要看输出里的失败详情。后台启动回执中的 `ok: true` 表示工具成功取得进程回执。如果状态是 `RUNNING`，这时 `succeeded` 仍为 `null`。查询或等待工具成功拿到观察结果时，也要看结果里的 `succeeded` 才能判断命令是否成功。[进程结果类型](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/ports/process.py) · [命令工具测试](https://github.com/tuwentao123/tsm-agt/blob/main/tests/test_core_process_tools.py)

不同 API 对同一份结果使用不同的消息格式，模型适配器负责转换。

| API | 模型发出调用 | Runtime 送回结果 | GitHub 源码 |
| --- | --- | --- | --- |
| OpenAI Chat | `assistant.tool_calls` | `role: "tool"`，用 `tool_call_id` 配对 | [Chat 适配器](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/adapters/openai_compatible/model.py) |
| OpenAI Responses | `function_call` | `function_call_output`，用 `call_id` 配对 | [Responses 适配器](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/adapters/openai_compatible/responses.py) |
| Anthropic Messages | `tool_use` 内容块 | `tool_result` 内容块，用 `tool_use_id` 配对 | [Messages 适配器](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/adapters/anthropic_messages/model.py) |

**日志分两层限制。** 执行器控制每条输出流保留多少字节，默认各保留 1,000,000 字节。一次工具结果每条流最多返回 16,000 字节的 UTF-8 文本。`has_more` 表示还有已保留的内容，可以用原 `process_id` 和对应的 `next_cursor` 调用 `core.process_logs`。`retention_truncated` 表示采集时已有输出超出保留上限，继续翻页只能读到已保留的部分。[日志分页](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/ports/process.py) · [日志工具](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/adapters/builtin/process_tools.py)

## 5. 后台命令结束后，原会话怎么继续

### 5.1 后台进程从哪里来

| 模型调用 `core.run_command` 时传了什么 | 工具何时返回 |
| --- | --- |
| 显式传 `mode: "background"` | **模型直接启动后台命令。** 工具启动并登记进程后就返回，通常带 `process_id` 和 `RUNNING`。 |
| 用前台模式，显式传入兼容参数 `timeout_seconds` | **先前台观察，再由 Runtime 接着托管。** 观察窗口结束时进程还在运行，工具就带着原 `process_id` 返回。同一个进程继续执行，无需再启动一次。 |
| 用默认前台模式，省略 `timeout_seconds` | 工具一直等到进程进入终态，再返回退出状态和输出。命令运行得久也会留在这次工具调用里。 |

前两种调用都启用退出通知。拿到运行中的回执后，模型可以继续独立工作，比如一边等编译，一边检查测试代码。Runtime 异步等待同一个进程，拿到退出状态、退出码和输出后，先持久化终态，再通过 `on_background_process_completed` 通知 SDK 调度器。**调度器是在终态落库之后检查会话状态的。** 如果进程很快退出，工具可以直接返回终态和输出，调度器会把这份已由工具返回的结果排除在待通知列表之外。[进程启动与回执](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/kernel_tool.py) · [后台等待、终态记录与通知](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/kernel_process.py)

### 5.2 SDK 怎么判断原会话空闲

`_wake_for_process_completion()` 持有会话输入锁和提交锁，重新读取最新的会话和任务状态。同时满足以下条件，才会自动开一轮。

1. 会话状态是 `ACTIVE`，进程所属的会话任务仍是该会话的当前任务。
2. 会话没有待处理的交互请求，任务状态是 `AWAITING_INPUT`，也就是上一轮已经结束、正在等下一条输入。
3. SDK 的 `_run_tasks` 里没有这个任务尚未结束的 runner。runner 是执行整轮模型请求和工具调用的异步任务。
4. 至少有一条尚未交付的进程终态可送给模型。

满足条件时，SDK 在释放锁之前把任务切回 `EXECUTING` 并登记新一轮的 runner。这样，即使用户刚好也发来消息，输入入口也能看到这轮已启动，并走正常的会话输入流程。如果进程结束时旧 runner 还没收尾，调度器会先保留结果。旧 runner 退出后，`_runner_finished()` 再安排一次检查，覆盖「进程刚退出，上一轮也正要结束」的时机。[空闲判定与 runner 收尾](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/sdk/runtime.py)

### 5.3 同一个退出结果何时进入模型请求

| 进程结束时的会话状态 | 模型何时收到结果 |
| --- | --- |
| 工具正在等这个进程 | 等待工具直接返回退出状态和日志，当前轮接着执行。 |
| 模型正在生成，或正在执行其他工具 | 先记录完成事件，在当前步骤边界送入下一次模型请求。若有工具批次，就等该批次的结果收齐。 |
| 原会话满足上面的空闲条件 | Web 和 SDK 在原会话自动开一轮。模型看到完成事件后，可以汇报结果或继续原任务。 |
| 正在等审批或用户回答 | 先保留完成事件，等会话恢复后再交付。 |

后台完成事件包含 `process_id`、退出状态、退出码和简短日志。它作为带有 Runtime 来源标记的上下文进入模型请求。模型需要更多日志时，可以再调用 `core.process_logs`。独立投递的完成事件会按进程 ID 记录交付，避免再次触发续轮。网页会在实时进度和历史记录里标出「后台任务完成，继续会话」。[会话投递边界](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/conversation_loop/loop.py) · [交付记录](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/conversation_loop/finalizer.py) · [空闲续轮调度](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/sdk/runtime.py) · [网页进度](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/web/app.py)

本地用测试模型驱动一条耗时 90.05 秒的前台命令。整轮发起了一次命令工具调用，进程启动、退出各记录一次，等待期间模型轮询次数为 0。后台路径的回归用例另外验证了两种情况：进程在读取工具执行中退出，结果随读文件结果进入下一次模型请求；进程在会话空闲后退出，同一会话自动续轮并通过事件流返回结果。[命令工具回归](https://github.com/tuwentao123/tsm-agt/blob/main/tests/test_core_process_tools.py) · [会话续轮回归](https://github.com/tuwentao123/tsm-agt/blob/main/tests/test_conversation_loop.py)
