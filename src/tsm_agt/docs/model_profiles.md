# 模型选择与 API 能力探测

真实 endpoint、模型 ID、凭据从仓库外的 `~/.tsm-agt/config.yaml` 读取。应用不会内置公司网关、内网地址、自动抓取模型目录或上传工作区做能力探测。示例见仓库根目录 `examples/model-profiles.yaml`，其中只有回环地址与占位值。

## 配置与选择

`model.providers` 声明 endpoint、auth 和明确的协议；`model.profiles` 是日常可选择的模型列表，声明模型、label、enabled 和 options。每个模型保留一项，绑定一个 provider，使用该 provider 固定的协议，不在模型列表中展开不同协议的测试组合。Profile ID 使用稳定的模型标识，无需附加协议后缀。

支持 `openai_chat`、`anthropic_messages`、`openai_responses`，具体使用哪一种由私有配置明确指定，不能仅按模型名称推断。模型选择区显示模型名称；用户选择模型后，服务按固定配置调用，不需要再选协议。不同模型可以共享相同协议的 provider。

Web 输入区从本地 `/model-profiles` 读取安全目录。此路由不调用上游 `/models`，不返回 base_url、auth 或原始 options。

`/models` 只是某些兼容服务采用的相对路径，不代表任意网关都实现了它。本项目不依赖该路径发现模型；真实模型列表及地址都由私有配置提供。

会话内可随时切换模型，已有消息和正在生成时也可操作。已有会话的选择立即保存到 Session，刷新后恢复；新会话的选择在首次发送时保存。切换从下一轮生效，正在生成、等待审批或暂停后恢复的轮次继续使用该轮快照中的模型与思考设置。快速连续切换按顺序保存，以最后一次选择为准；保存失败会恢复已确认的选择并显示错误。

每轮开始时将模型选择复制到 Turn Checkpoint 和该轮配置快照，不修改进程全局模型，并发会话互相隔离。切换时清除新模型不支持的思考档位。历史文本与工具记录保留；不透明推理块标记来源 Profile，只回传给原 Profile，不向其他模型发送签名或加密推理块。旧推理块缺少来源标记时不跨 Profile 重放。

当前选择针对主对话执行模型；意图路由、规划和完成复核仍使用部署默认 Profile。配置修改后需要重启进程。Session 保存 Profile ID，不冻结凭据配置；修改同名 Profile 的模型或协议时应使用新 ID，保留旧会话原配置。历史 Session 没有 Profile 字段时兼容当前默认模型。

`enabled: false` 保留目录项但禁止调用；`kind: image` 禁止用作代码 Agent。`reasoning_efforts` 控制 UI 可选档位，是运维声明，不是能力探测结果。没有列表时只提供服务默认设置。

`enabled` 是是否允许选择，`status` 是验证结论，两者分开。`status` 可为 `ready`、`limited`、`unverified`、`blocked`、`unsupported`，说明放在 `status_detail`；未配置时为待验证。一次参数不兼容或某项能力未通过，不能推导为整个模型不可用。界面分别显示待验证、部分能力受限、服务受限、协议未接入或手动未启用。

已验证只能文本调用的路由可声明 `capabilities: {tools: false, parallel_tools: false}`。会话仍可进行文本问答，不发送工具定义，提示当前模型不能操作工作区；若供应商意外返回工具调用，会拒绝执行。需要工具的工作项不会使用文本模式执行。

## 思考与缓存

- Chat 支持 reasoning_effort；原生 thinking 兼容服务可配置 options.request_parameters.thinking，这时不重复发送 reasoning_effort。UI 的 none/high 分别切换 disabled/enabled，不承诺原生 thinking 有多个强度档。
- Messages 支持 thinking、output_config.effort、cache_control。none 关闭 thinking，其他强度默认采用 adaptive，也可配置 legacy enabled/budget_tokens。供应商的 token 预算限制仍适用；拒绝参数时不会静默删除并假称成功。
- Responses 使用 reasoning.effort，支持每轮覆盖，保留缓存与思考 token 统计及不透明 reasoning 历史。
- Chat reasoning_content、Messages thinking/signature/redacted_thinking 作为 provider-owned block 重放，不作为用户回答或思考摘要展示。
- request_parameters 使用协议白名单，不能覆盖 model、messages、input、认证头等身份或历史数据。
- 缓存接受参数与实际命中分开报告。观察 cached_tokens 或 cache_read_input_tokens 等计数，不能用响应速度证明缓存命中。
- 可选的 `options.send_output_token_limit: false` 让 Chat/Responses 不发送输出 token 上限，适用于已确认存在参数转换问题的兼容网关。框架上下文预算不变，上游输出长度使用服务默认设置；默认仍发送上限。Messages 必须提供上限，不接受此选项为 false。此选项按 Profile 明确配置，不根据报错静默重试。

## 可重复的真实 API 探测

探测只发送合成提示、工具定义、算术题和稳定缓存前缀，不读取或发送项目文件，不执行模型返回的工具调用。调用需显式允许：

```bash
.venv/bin/python scripts/probe_model_capabilities.py \
  --profile default-model \
  --feature text --feature tools --feature stream --feature reasoning --feature cache \
  --output .agent/evaluations/model-probe/report.json --allow-model-calls
```

`--config` 指定其他私有配置，`--profile` 可重复。省略 `--protocol` 按 Profile 的固定协议调用；协议对照测试通过显式重复 `--protocol openai_chat|anthropic_messages|openai_responses` 完成，不创建重复 Profile、不修改生产配置。测试其他协议时，目标网关必须支持该路由及认证方式。

工具检查合成函数名和参数，不等于完整 Agent 多轮工具验证；流式检查正文和结束标记；思考只测试 Profile 声明的 reasoning_efforts（缺少列表时使用已配置的默认档位），不会统一向所有模型发送 none。没有任何已声明档位时记为待验证，不猜测支持范围。每个档位独立记录参数错误或超时，观察推理块或 reasoning_tokens；缓存连续发送两次稳定前缀，观察命中计数。一次未命中不能证明不支持缓存。

状态：observed（观察到行为）、accepted_unobserved（接受请求但缺少证据）、not_observed（未取得期望结果）、unverified（缺少测试契约）、rejected（该请求组合被拒绝）、error（传输或解析错误）。错误单列参数不兼容、工具路由不兼容、账号权限、服务下线及协议不支持；不自动改写 enabled。`--include-disabled` 可复测被禁用的文本 Profile，不修改私有配置。记录请求模型与返回 model 标识，后者是网关声明，不能保证物理后端身份。报告不保存 endpoint、凭据、原始回复或推理正文。

长前缀缓存测试会产生输入 token 费用。最多 3 个任务并发，每个 HTTP 请求有超时。模型行为质量需使用相同任务、工具、权限和预算另作轨迹评测。
