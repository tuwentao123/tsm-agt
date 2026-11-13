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

主对话的上下文预算仍预留输出空间，这份预留不会自动成为请求的输出上限。OpenAI Chat / Responses 默认省略输出上限，由服务端决定；需要限制时设置模型的 `options.max_output_tokens`（正整数）。本地预算会至少预留该上限。Anthropic Messages 必须发送 `max_tokens`，未配置时使用 8,192。`send_output_token_limit: false` 仍可用于 OpenAI 接口，但不能与显式 `max_output_tokens` 同时配置。

修改 YAML 后重启服务。更多选项见[配置示例](examples/model-profiles.yaml)。

MCP 通过 `core.mcp_discover` 按需发现、`core.mcp_call` 调用，Skill 通过 `skill.discover` 查询、`skill.load` 读取。工具结果随对话保留，上下文窗口在后续轮次和重启后复用；项目指令、模型配置变化或压缩会重建基线。

可选的上下文配置

```yaml
context:
  compaction_ratio: 0.8
  history_soft_tokens: 16384
  semantic_compaction: true
  summary_timeout_seconds: 180
  summary_request_timeout_seconds: 90
  summary_max_output_tokens: 2048
  summary_failure_cooldown_seconds: 60
  skill_catalog_ratio: 0.02
  skill_catalog_fallback_tokens: 2048
```

`history_soft_tokens` 用于新会话窗口装入旧历史，已有窗口按容量或显式配置的延迟阈值触发压缩。
被移出的历史可通过 `runtime.read_history` 在当前会话和工作区内按引用回查。

## 从 Kiro 反代获取模型

只使用 Kiro 反代时，本机配置可以写成下面这样。把地址和密钥换成 Kiro 面板里的值。

```yaml
version: 1
model:
  providers:
    kiro:
      base_url: http://127.0.0.1:5580
      auth: {api_key: your-api-key}
      discover_models: true
```

启动时，tsm-agt 从反代的 `/v1/models` 读取模型列表，在会话的「模型」下拉框里显示。选择在下一轮生效。默认通过 Chat Completions 调用。Kiro 的模型列表变化后，重启 tsm-agt 即可重新读取。

已有直连模型时，在 `model.providers` 里新增独立的 `kiro` 条目，并保留原有的 `profiles` 和 `default_profile`。Kiro 配置有误或模型列表读取失败时，可在网页的 Runtime 设置里查看提示，直连模型仍可使用。自动补齐的 `/v1` 路径和协议默认值只应用于自动发现的模型。

## 在 Langfuse 查看评测

在 Langfuse 项目设置中创建 API 密钥，并保存到本机的 `~/.tsm-agt/langfuse.yaml`。
美国区实例的配置如下，自托管实例填写自己的服务地址。

```yaml
base_url: https://us.cloud.langfuse.com
public_key: "pk-lf-..."
secret_key: "sk-lf-..."
```

将文件权限设为 `chmod 600 ~/.tsm-agt/langfuse.yaml`。`--langfuse-config` 可以指定其他文件。
缺少默认 YAML 时，现有脚本仍可使用 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 和
`LANGFUSE_BASE_URL`。

已有评测报告可以直接导入，命令会打印每条试跑的调用链链接。

```bash
uv run python -m scripts.export_langfuse_evaluation /path/to/baseline.jsonl
```

运行新评测时加上 `--langfuse`。

```bash
uv run python -m scripts.evaluate_multiturn_behavior \
  --config "$HOME/.tsm-agt/eval-config.yaml" \
  --fixture scripts/fixtures/multiturn-implementation.json \
  --fixture scripts/fixtures/replay-podfile-local.json \
  --label baseline --repeat 1 --allow-model-calls --run-tests --langfuse \
  --report-path baseline.jsonl
```

在 Langfuse 的 **Experiments** 中选择本次实验，再打开一条试跑查看轮次、模型响应、
工具结果和布尔评分。对比候选版本时，用同一批用例和重复次数，并给候选版本设置不同的
`--label`。导出包含用例标识、模型、工具名、退出码、token 用量和评分。原始对话、
工具参数、工具输出正文与密钥保留在本机。历史报告缺少的耗时在界面上显示为零。

上传失败时，本地 JSONL 仍可用于重试。同一份报告重试时复用实验、调用链和评分 ID。
实例需要支持 [OpenTelemetry 实验导入](https://langfuse.com/integrations/native/opentelemetry/experiments)。

### 在 Langfuse 查看日常 Web 会话

启动 `tsm-agt-web`，选择一个会话，在输入区勾选「Langfuse 记录」。开关默认关闭，
浏览器刷新后保留当前状态，重启 Web 服务后需要重新开启。Web 端从同一个本地 YAML
读取项目配置。

勾选后，新轮次在结束时自动发送 Trace。在 Langfuse 的 **Sessions** 中可以找到同一会话的
多轮 Trace。在 **Tracing** 中按 `web` 环境筛选，展开 Trace 后点击某个节点，在 **Preview**
查看执行摘要，包括轮次状态、模型响应的 token 用量和停止原因、工具类别和执行结果。
对话原文、项目路径、工具名、工具参数和输出正文留在本机。

关闭开关后，尚未发起的上传会跳过。已发送的 Trace 保存在 Langfuse。发送失败会在
Web 服务日志中记录，日常会话可以继续。日常 Trace 不含评测分数，离线评测仍通过
上面的评测命令运行。
