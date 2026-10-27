# 从 0 到 1：搭建一个支持多轮会话的 Coding Agent

做一个能回答代码问题的聊天机器人并不难；难的是让它能在真实仓库里连续工作：读文件、调用工具、修改代码、执行验证，并且在用户下一次输入后还能接着做。

本文用 TSM-Agent 的实现思路，简单介绍一个多轮 Coding Agent 的最小架构。

项目源码：[GitHub · tuwentao123/tsm-agt](https://github.com/tuwentao123/tsm-agt)

## 先确定四个对象

一个实用的 Coding Agent 不应只有「消息列表」。推荐把运行时拆成四层：

```text
Session → Task → Turn → Model / Tool Call
```

- **Session**：一段用户会话，负责保存可见的聊天历史。
- **Task**：一个轻量的执行单元，关联工作区、权限、工具执行记录和临时状态。
- **Turn**：用户的一次输入触发的一轮 Agent 工作。
- **Model / Tool Call**：模型思考与工具执行的最小步骤。

这样做的好处是：聊天上下文、执行状态和工具副作用不再混在同一段 Prompt 里。Session 用来延续对话；Task 用来承载执行；Turn 用来定义一次输入的开始和结束。

这里的 Task 当前刻意保持轻量：它不承担完整的项目管理、计划生成或复杂目标编排，只定义一次工程执行的边界并保存事实。这样用户的普通对话不必先落入一个重型任务模型；同时也为后续接入 Goal、长任务恢复、步骤依赖和多 Agent 协作预留了稳定的扩展点。

## 第一步：让模型只负责“下一步做什么”

Coding Agent 的模型职责可以保持很小：根据当前用户目标、上下文和工具描述，决定是否调用工具、调用哪个工具、看到结果后如何继续，以及何时给出回答。

Runtime 则负责模型不应该自行决定的事实：

- 当前 Task 是否允许执行；
- 工具是否存在、参数是否合法；
- 操作是否需要用户审批；
- 工具结果、文件改动和事件如何持久化；
- 调用是否超时、取消或达到预算上限。

一句话概括：**模型负责决策，Runtime 负责约束、执行和记录事实。**

## 第二步：搭建最小 Agent Loop

一次 Turn 的核心循环很直接：

```text
用户输入
  ↓
组装 Prompt + 可见工具
  ↓
调用模型
  ├─ 没有工具调用 → 生成最终回复
  └─ 有工具调用 → Runtime 执行工具 → 写回 Tool Result → 再调用模型
```

TSM-Agent 中，模型看到的是标准的 `user`、`assistant`、`tool` 消息。工具执行结果不会靠字符串拼接回 Prompt，而是作为 `tool` 消息回传，这样模型能明确区分“用户说了什么”和“工具实际观察到了什么”。

最小伪代码如下：

```python
while True:
    response = await model.complete(messages, tools=visible_tools)

    if not response.tool_calls:
        return response.text

    results = await runtime.execute(response.tool_calls)
    messages.extend([response.message, *results])
```

真正上线时，需要在 `execute` 前后加入权限、超时、审计、幂等和结果治理，但主循环并不需要变复杂。

## 第三步：把“多轮”做成持久化状态，而不是多塞几条历史消息

多轮会话最重要的不是无限累积 Prompt，而是每轮结束后把必要状态写下来。

TSM-Agent 会把用户输入、Turn 开始、模型调用、工具执行、审批、最终回复等写入事件存储；Session 上下文再由这些事实重建。这样页面刷新、进程重启后，仍能恢复同一个会话。

下一轮模型请求通常包含：

```text
系统规则
+ 项目/工作区上下文
+ Session 中可见的历史对话
+ Task 的工作记忆和已完成事实
+ 当前用户输入
```

其中，工具原始输出、凭据、审批令牌和隐藏推理不应直接进入 Session 历史。它们属于 Task 的受控执行记录，而不是普通聊天内容。

## 第四步：支持用户在执行中继续输入

真实用户不会总等一轮完全结束。比如 Agent 正在跑测试，用户又补充：“顺便检查错误路径”。

TSM-Agent 现在采用两种明确模式：

1. **默认输入：中断并转入同一 Task 的下一轮。**
   Runtime 先取消当前 Turn，等旧 runner 收敛，再以新输入启动 fresh turn。已完成的工具调用、文件改动和事件仍保留；未完成的模型推理和执行现场不继续复用。

2. **显式 `STEER`：在安全边界注入当前 Turn。**
   如果用户或客户端明确选择 steering，输入先进入持久队列；正在执行的一批工具完成后，或下一次模型调用前，再把它放入模型上下文。

这两个模式分别覆盖“换一个方向继续做”和“给当前工作补充信息”。无论是哪一种，都不需要 Runtime 去猜用户文本的语义关系。

## 第五步：把验证当成一次真实工具调用

Coding Agent 修改代码后，不能只说“应该可以”。验证要有可观察证据，例如：

- `pytest`、`npm test`、`npm run build`；
- 编译、类型检查、lint；
- 浏览器自动化或接口调用；
- MCP 工具返回的结构化结果。

模型决定是否需要验证以及选择哪种验证；Runtime 保存命令退出状态、工具结果和相关证据。最终回复可以说明“运行了什么、结果是什么、覆盖了什么”，但不能把“命令成功启动”误写成“验证通过”。

## 第六步：接入 MCP，让外部能力成为普通工具

MCP 不需要成为另一套 Agent Loop。把 MCP Server 发现到的工具转换为统一的 ToolSpec，再按普通工具执行即可：

```text
MCP 配置 → 建立连接 → tools/list → 注册为模型可见工具
                                      ↓
模型调用 → Runtime 权限/审批 → MCP call_tool → Tool Result
```

因此，文件系统工具、Shell、浏览器和 MCP 工具可以共享同一套调用记录、风险控制和结果回传机制。TSM-Agent 的具体配置可参考 [MCP 集成说明](../src/tsm_agt/docs/mcp_integration.md)。

## 一个最小使用示例

应用层只需要维护稳定的 `session_id`：

```python
from pathlib import Path

from tsm_agt.sdk import EngineeringAgentClient


async with EngineeringAgentClient(Path.cwd()) as client:
    accepted = await client.submit_user_input(
        "session-demo",
        "帮我定位登录接口的超时原因，并给出修复建议",
        input_id="input-001",
    )
    task_id = accepted.result["task"]["task_id"]
    result = await client.wait_task(task_id)
    print(result.assistant_text)
```

后续继续向同一个 `session-demo` 提交输入，就得到多轮对话。模型能利用已持久化的会话上下文继续工作，而 Runtime 始终掌握工具、文件、权限和验证的边界。

## 小结

从 0 到 1 搭建多轮 Coding Agent，核心并不是堆更多 Prompt，而是建立清晰的运行时边界：

```text
Session 保存对话
Task 保存执行事实
Turn 表示一次用户请求
Model 选择下一步
Runtime 安全地执行、持久化和验证
```

把这几层分开后，Agent 才能既像聊天一样连续，又能像工程工具一样可靠地操作真实项目。

## 后续可扩展能力

当前实现聚焦于多轮会话、工具调用和可验证的工程执行。后续还可以继续完善：

- 更完整的沙箱与权限隔离机制；
- Shell、浏览器、代码检索、MCP 等工具能力及其治理；
- 图片、音频、视频等多模态模型与工具支持；
- 按任务复杂度、成本和能力选择模型的模型路由；
- 在执行前识别用户意图、任务类型和所需工具的前置路由。
- Goal 能力：将长期目标拆分为可执行步骤，追踪进度、依赖与阻塞状态，并在会话或进程恢复后继续执行。
