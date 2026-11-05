# MCP 集成

TSM-AGT 使用官方 Python MCP SDK，把已配置的 MCP server 变成模型可见的
`mcp.<server-id>.<tool-name>` 工具。MCP 是项目依赖的一部分；安装项目时会一并
安装，不会在 Agent 运行时执行 `pip install`。

本文描述当前已实现的行为。与 Codex、Hermes、DeepSeek Harness 的源码对比及待实施
方案见 [MCP 设计审查](mcp_design_review.md)。

## 安装

```sh
python -m pip install -e '.[dev]'
```

`pyproject.toml` 声明依赖为 `mcp[cli]>=1.12,<2`；这是版本范围，不是固定版本。
缺少 SDK 时，配置了 MCP 的 provider 会在启动时明确失败。

## 配置一个 server

在本机私有配置 `~/.tsm-agt/config.yaml` 中增加以下内容；保留原有的模型配置：

```yaml
mcp:
  enabled: true
  servers:
    demo:
      enabled: true
      kind: executable
      command: [python, -m, tsm_agt.adapters.mcp_demo_server_stdio]
      options:
        cwd: /absolute/path/to/tsm-agt
```

CLI 的 `--config /absolute/path/to/config.yaml` 可以选择另一份完整私有配置。
当前 production composition 直接读取该 YAML 的 `mcp.servers`；不会自动合并工作区
`.tsm-agt/mcp.json`、`~/.config/tsm-agt/mcp.json`，也不通过
`TSM_AGT_MCP_CONFIG`、`TSM_AGT_ENABLE_MCP_TOOLS` 选择或开关 MCP。
代码保留了 JSON 解析、合并辅助函数，供显式调用使用，它们不是默认配置入口。

支持的 transport：

- `executable`：通过 stdio 启动 `command` 数组；`options.env` 为字符串环境变量映射，
  `options.cwd` 为子进程启动目录。命令不经 shell 解析；程序与参数应分开填写。
- `http` 或 `streamable_http`：使用 `url`；可选 `options.headers` 和
  `options.timeout_seconds`，后者默认 30 秒，传给 SDK 的 HTTP transport。
- `inprocess`：仅自定义工厂/fixture 使用，production 官方 SDK 工厂不支持。

`mcp.enabled: false` 关闭全部 MCP 注册；单个 server 的 `enabled: false` 跳过该项。
工厂会把裸 `python` / `python3` 改成 Agent 的 Python 解释器以保持兼容；显式解释器
路径、其他程序和参数保持原样。单元素 `command: [my-mcp-server]` 直接启动该程序，
不推断为 Python 脚本。Python 脚本应明确配置 `[python, /absolute/path/server.py]`。

## 从启动到模型调用

1. `compose_openai_compatible_engineering_application_from_env` 读取私有 YAML。
2. 有已启用的 server 时，composition 注册 `MCPToolProvider` 和 `MCPClientManager`。
3. `Application.start()` 经 registry 启动 provider。每个 MCP 在独立后台 Task 中建立 transport、
   由每连接一个长期存活的 owner Task 进入 `ClientSession` 并执行 `initialize`；provider
   随后发现工具。连接的创建和退出始终由该 owner 完成，其他 Task 可以调用或请求关闭。
   默认 `required: false`，核心 Runtime 不等待 MCP。每服务器默认
   `startup_timeout_seconds: 30`，覆盖连接、initialize 和完整工具发现；初始化、发现或
   schema 失败仅将该服务器标为 failed，并清理其连接，其他服务器和聊天继续运行。
   只有显式配置 `required: true` 才等待其就绪，并在失败时阻止 Runtime 启动。
4. SDK 的 `inputSchema` 被转换为 Runtime `ToolSpec`。发现会遍历 `tools/list` 的全部
   分页；重复/无效游标、超过 1000 页或 10000 个工具会明确失败，不发布半份目录。
   这两个界限用于阻止无界发现，不是 MCP 标准规定。每服务器首次发现成功后整体发布，
   下一次 Turn 取工具快照时可见；不会修改已经开始的 Turn 的工具快照。
   暂不处理运行中的 `tools/list_changed`。
5. 模型收到例如 `mcp.demo.add` 的参数 schema，自行决定是否调用。
6. Kernel 按普通外部工具执行参数校验、风险与审批策略、执行账本和超时控制。MCP 工具
   当前统一为 R2、非只读、不可并发、非幂等；服务器声明不会自动降低权限。
7. Provider 调用 MCP `call_tool`，将 SDK 返回值转换为可持久化 JSON，作为同一次
   Agent loop 的 `ToolResult` 回注到下一次模型请求。`isError` 会成为失败结果；服务端
   内容按外部不可信数据处理。连接获取失败或客户端明确证明未发送时，返回未派发错误；
   调用后断连、超时或成功结果校验/序列化失败时，保守返回 `UNKNOWN_OUTCOME`。
   Kernel 将其写入结果未知账本，Agent loop 沿用现有的现场核对暂停流程，不自动重放操作。
8. 宿主停止时调用 `application.registry.stop_all()`，provider 再关闭 session 与 transport。
   关闭者等待 owner 完成清理；重复关闭共享同一次清理。初始化中取消会清理后继续传递
   `CancelledError`；关闭中被取消也先等待清理再传递取消。manager 还会等待已在进行的初始化。

现有 Kernel 调用有工具 deadline；HTTP transport 的 `timeout_seconds` 不等于独立的
MCP 启动预算或每个 MCP 工具的执行预算。`/diagnostics` 只查询当前实例的 MCP 状态，
失败提示保留服务器 ID、阶段和 HTTP 状态码，避免展示 AnyIO 的内部 `WouldBlock`。
目前没有自动重连、
MCP OAuth 登录、resources/prompts 的模型入口，也没有专门的多媒体结果投影或输出上限。

Runtime 只负责连接、权限、调用及回传事实；选择哪一个 MCP 工具、怎样根据结果继续、
以及是否需要进一步验证，都由模型决定。

## 本地端到端检查

在项目虚拟环境中执行真实 stdio demo，无须连接用户的远程 MCP：

```sh
python -m tsm_agt.adapters.mcp_sdk_demo_verify
python -m pytest -q tests/test_mcp_sdk_integration.py
```

第一条执行 initialize、工具发现、`echo` / `add` 调用和关闭。回归测试还覆盖了指定
私有 YAML 进入 production composition、模型发起 `mcp.demo.add`、审批恢复后的结果
回注、跨 Task 使用/关闭、真实分页，以及写入后响应丢失或结构化结果无效时的结果未知、
暂停和恢复不重放。其他针对性回归：

```sh
python -m pytest -q tests/test_mcp_lifecycle.py tests/test_mcp_call_outcomes.py
python -m pytest -q tests/test_mcp_http_integration.py
```

HTTP 回归仅监听本机回环地址，覆盖有状态 SSE 与无状态 JSON 两种 Streamable HTTP
往返和关闭。受限环境需要允许创建本地监听端口。上述检查不验证远程服务器的 OAuth、
目录刷新或自动重连，这些能力仍在后续阶段。
