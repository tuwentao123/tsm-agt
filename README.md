# tsm-agt

本地工程 Agent，支持看代码、修改文件、运行命令和验证结果，提供 Web 与 CLI。

## 快速开始

需要 Python 3.12 和 uv，在仓库根目录执行：

```bash
uv sync
uv run tsm-agt init
```

编辑本机配置 `~/.tsm-agt/config.yaml`，填入模型服务信息；已有配置可直接编辑。随后启动：

```bash
uv run tsm-agt doctor --workspace . --model-check
uv run tsm-agt-web
```

打开 [Web 页面](http://127.0.0.1:8080)，选择工作区和模型后开始对话。也可使用命令行：

```bash
uv run tsm-agt chat --workspace .
```

## YAML 配置

最小示例；地址、密钥和模型名均需替换为实际值，真实配置只放本机文件：

```yaml
version: 1
model:
  default_profile: main
  providers:
    gateway:
      protocol: openai_chat
      base_url: http://127.0.0.1:8000/v1
      auth:
        api_key: your-api-key
  profiles:
    main:
      label: 默认模型
      provider: gateway
      model: your-model-name
      options:
        streaming: true
        strict_tool_schema: false
        output_token_parameter: max_completion_tokens
```

- `providers`：服务地址、认证及协议，协议可选 `openai_chat`、`openai_responses`、`anthropic_messages`。
- `profiles`：每个模型一项，`provider` 引用服务配置，`model` 填服务端模型 ID；增加模型时新增一项即可。
- `default_profile`：默认模型的配置名，此处为 `main`。网页切换模型从下一轮生效。
- `options`：按接口支持填写；Chat 输出上限参数使用 `max_tokens` 或 `max_completion_tokens`。

主对话的上下文预算仍预留输出空间（默认 8,192 tokens），这份预留不会自动成为请求的输出上限。OpenAI Chat / Responses 默认省略输出上限，由服务端决定；需要限制时设置模型的 `options.max_output_tokens`（正整数）。本地预算会至少预留该上限。Anthropic Messages 必须发送 `max_tokens`，未配置时使用 8,192。`send_output_token_limit: false` 仍可用于 OpenAI 接口，但不能与显式 `max_output_tokens` 同时配置。

修改 YAML 后重启服务。更多选项见[配置示例](examples/model-profiles.yaml)和[模型配置说明](src/tsm_agt/docs/model_profiles.md)。
