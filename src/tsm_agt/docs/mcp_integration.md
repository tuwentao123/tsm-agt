# MCP 集成

TSM-AGT 使用官方 Python MCP SDK，把已配置的 MCP server 变成模型可见的
`mcp.<server-id>.<tool-name>` 工具。MCP 是项目依赖的一部分；安装项目时会一并
安装，而不是在 Agent 运行时静默执行 `pip install`。

## 安装

```sh
python -m pip install -e '.[dev]'
mcp version
```

`pyproject.toml` 固定依赖为 `mcp[cli]>=1.12,<2`。启动时缺少 SDK 会使 MCP
provider 的初始化明确失败；模型不会看到一个不能调用的 MCP 工具。

## 配置一个 server

在工程根目录创建 `.tsm-agt/mcp.json`：

```json
{
  "mcpServers": {
    "demo": {
      "kind": "executable",
      "command": ["python", "-m", "tsm_agt.adapters.mcp_demo_server_stdio"],
      "options": {
        "cwd": "."
      }
    }
  }
}
```

也可以设置 `TSM_AGT_MCP_CONFIG=/absolute/path/to/mcp.json`，它会覆盖默认
发现路径。默认情况下按“用户配置、工作区配置”的顺序合并，后者覆盖同名 server：

- `~/.config/tsm-agt/mcp.json`
- `<workspace>/.tsm-agt/mcp.json`

支持的 transport：

- `executable`：通过 stdio 启动 `command`；`options.env` 是字符串环境变量映射，
  `options.cwd` 是启动目录。
- `http` 或 `streamable_http`：使用 `url`；可选 `options.headers` 和
  `options.timeout_seconds`。

`TSM_AGT_ENABLE_MCP_TOOLS=0` 会完全关闭 MCP 工具注册。未设置或设置为
`1`、`true`、`yes`、`on` 时启用。

## 从启动到模型调用

1. `compose_openai_compatible_engineering_application_from_env` 读取 MCP 配置。
2. production composition 为配置的 server 注册 `MCPToolProvider`。
3. `Application.start()` 建立 transport、进入 `ClientSession` 上下文并执行 MCP
   `initialize`，随后调用 `list_tools`。连接、初始化或 schema 有问题会在启动时失败。
4. 官方 SDK 的 `inputSchema` 被转换为 Runtime `ToolSpec`，模型收到例如
   `mcp.demo.add` 的 JSON schema。
5. 模型调用该工具后，Kernel 仍按普通外部工具实施风险与审批策略；MCP server 不会绕过
   审批。当前默认风险为 R2。
6. Provider 调用 MCP `call_tool`，把 SDK 的 Pydantic 结果转换为可持久化 JSON，写成
   同一次 Agent loop 的 `ToolResult`。下一次模型采样直接收到该结果。
7. `Application.stop()` 依次关闭 `ClientSession`、stdio 子进程或 HTTP transport，避免
   残留 MCP server。

Runtime 只负责连接、权限、调用及回传事实；选择哪一个 MCP 工具、怎样根据结果继续、
以及是否需要进一步验证，都由模型决定。

## 本地端到端检查

仓库包含一个真实 stdio demo server。下面的命令会执行 initialize、工具发现和两次工具
调用：

```sh
python -m tsm_agt.adapters.mcp_sdk_demo_verify
```

自动回归 `tests/test_mcp_sdk_integration.py` 还覆盖了：工作区配置进入 production
composition、模型实际发起 `mcp.demo.add`、审批恢复后结果回注到下一次模型调用，以及
会话关闭。
