# tsm-agt

`tsm-agt` 是一个遵守 Ports & Adapters 的通用工程 Agent Harness。当前已具备 Task/Turn 状态机、SQLite 原子事件落盘、串行 Agent Loop、审批和幂等账本、Project Trust、受控进程、工作区 Mutation Journal/回滚，以及 macOS/Windows 平台 Adapter。

当前 Agent Loop 可以完成“模型提出 Tool Call → Runtime 执行 → Tool Result 回填 → 模型继续回答”的 ReAct 闭环。工程版提供受控文件读取/搜索、结构化命令、后台进程、Patch/删除和安全回滚工具，并支持 OpenAI-compatible Chat Completions API。Git 可按需通过受控命令使用，不作为独立核心 Tool。并行 Tool Call、完整实时 Web UI、MCP 和领域扩展仍待后续步骤实现。

Coding Agent 主闭环已经加入可信收口：Chat 不会因为模型说“完成”就直接把 Task 标成成功。只读 Task 由 Verifier 确认没有待核验 Mutation；写入 Task 必须同时满足当前文件 Hash 仍与 Mutation Journal 一致，以及最后一次写入后最新的、被识别为构建/测试/静态检查的前台命令 `exit_code=0`。任一准则失败或缺失时 Task 进入 FAILED，并显示 `verification: failed/blocked`，不会冒充成功。

Event Log 已可通过纯 Core `FlowProjector` 投影为 Task/Phase/Turn/Model/Tool/Approval/Process/Mutation/Memory/Onboarding 流程节点，并支持 cursor 增量更新和首个可行动失败定位。CLI 支持流程树、泳道时间线、静态/实时 TUI、下钻、筛选以及 JSON/JSONL/离线 HTML 脱敏导出；Replay 支持定位、范围自动播放、交互单步和断点游标恢复。Task 首次进入执行态时还会在同一 SQLite 事务中保存脱敏、不可变的 Effective Configuration revision 1。基础 Prompt Manifest 与确定性装配也已接入，每次模型调用记录不含正文的 `effective_prompt_hash`。完整实时 Web UI 与重新执行模型/副作用的 `--rerun` 仍待其他安全前置能力完成。

## 验证当前进度

首次进入项目先同步精确锁定的运行时依赖，之后统一通过项目环境启动，避免系统 Python 绕过终端输入组件：

```bash
uv sync
```

常用验证命令：

```bash
uv run tsm-agt doctor
uv run tsm-agt task-demo "修复失败测试" --workspace .
uv run tsm-agt turn-demo "解释这个错误" --workspace .
uv run tsm-agt tool-demo "hello tool" --workspace .
uv run tsm-agt agent-demo "inspect this" --workspace .
uv run tsm-agt resume <task_id> --workspace .
uv run tsm-agt approve <approval_id> --reason "reviewed" --workspace .
uv run tsm-agt memory list <task_id> --workspace .
uv run tsm-agt memory list <task_id> --include-stale --workspace .
uv run tsm-agt onboarding show <task_id> --workspace .
uv run tsm-agt onboarding run <task_id> --workspace .
uv run tsm-agt files-demo src --recursive --workspace .
uv run tsm-agt flow <task_id> --workspace .
uv run tsm-agt flow <task_id> --workspace . --json
uv run tsm-agt flow <task_id> --workspace . --live
uv run tsm-agt flow <task_id> --workspace . --live --json
uv run tsm-agt flow <task_id> --workspace . --node <node_id>
uv run tsm-agt flow <task_id> --workspace . --node <node_id> --json
uv run tsm-agt flow <task_id> --workspace . --failure
uv run tsm-agt inspect <task_id> --effective-config --workspace .
uv run tsm-agt inspect <task_id> --effective-config --revision 1 --json --workspace .
uv run tsm-agt flow <task_id> --workspace . --kind tool
uv run tsm-agt flow <task_id> --workspace . --kind tool --kind model --status FAILED
uv run tsm-agt flow <task_id> --workspace . --timeline
uv run tsm-agt flow <task_id> --workspace . --tui
uv run tsm-agt flow <task_id> --workspace . --tui --live
mkdir -p artifacts
uv run tsm-agt flow <task_id> --workspace . --export artifacts/flow.json
uv run tsm-agt flow <task_id> --workspace . --export artifacts/flow.jsonl
uv run tsm-agt flow <task_id> --workspace . --export artifacts/flow.html
uv run tsm-agt flow <task_id> --workspace . --kind tool --export artifacts/tools.json
uv run tsm-agt replay <task_id> --workspace . --list
uv run tsm-agt replay <task_id> --workspace . --at 12
uv run tsm-agt replay <task_id> --workspace . --at 12 --json
uv run tsm-agt replay <task_id> --workspace . --turn <turn_id> --boundary start
uv run tsm-agt replay <task_id> --workspace . --node <node_id> --boundary end
uv run tsm-agt replay <task_id> --workspace . --play --speed 2x
uv run tsm-agt replay <task_id> --workspace . --play --speed max --json
uv run tsm-agt replay <task_id> --workspace . --play --speed max --from 10 --to 20
uv run tsm-agt replay <task_id> --workspace . --play --speed max --step --save-cursor replay-cursor.json
uv run tsm-agt replay <task_id> --workspace . --play --speed max --resume-cursor replay-cursor.json
uv run python -m unittest discover -s tests -v
```

## 统一 Python / pytest / Web 启动策略

当前 Runtime 对命令执行有两个关键限制：

- `argv[0]` 使用 `./.venv/bin/python` 或 `.venv/bin/python` 时，Runtime 可能因为相对 executable 校验而拒绝执行。
- `cwd` 必须保持 workspace-relative；直接传入开发者本地绝对路径会触发 Runtime sandbox 拒绝。

因此项目不再把 `.venv/bin/python` 作为默认推荐入口，也不依赖固定 workspace 名称或开发者机器绝对路径。

统一策略如下：

- 使用 `python -m ...` module invocation，而不是硬编码 `.venv/bin/python`。
- 使用当前激活解释器的 `sys.executable`，让本地开发、CI、容器、systemd、Kubernetes、GitHub Actions 与 PaaS 环境都能复用同一入口。
- 所有项目命令都从 workspace root 启动，并由 `scripts/run_in_project_env.py` 自动补齐 `PYTHONPATH=src`。
- Web app 与 pytest 共用同一解释器来源，避免 reload、subprocess 或测试环境出现解释器漂移。
- Runtime 中推荐使用 workspace-relative `cwd=.`；不要绑定 `/Users/...` 之类的本地绝对路径。

推荐入口：

```bash
python scripts/run_in_project_env.py pytest tests -q
python scripts/run_in_project_env.py web
python scripts/run_in_project_env.py module unittest discover -s tests
```

适用边界与部署说明：

- 本地开发：兼容 `uv run`、激活 venv、direnv 或系统 Python shim。
- CI：只要求 PATH 中存在目标 Python，不依赖 `.venv` 目录名称。
- 容器：ENTRYPOINT 可以直接执行 `python scripts/run_in_project_env.py web`。
- systemd / supervisor / PaaS：可直接调用 `python -m tsm_agt.web.app` 或统一 launcher。
- pytest：统一走 module invocation，避免 Runtime 对相对 executable 的拒绝。
- reload：当前 `uvicorn.run(... reload=False)` 保持生产安全默认值；开发环境如需热重载，应在外部进程管理器或显式 dev 配置中开启，而不是依赖固定本地路径。

不推荐的方式：

```bash
./.venv/bin/python -m pytest
```

该方式在普通 shell 中通常可运行，但在 Runtime sandbox、远程执行器或容器编排环境中可能因为 executable/cwd 校验失败而不可移植。

## 安装与首次启动

开发项目时使用 `uv sync` + `uv run tsm-agt`；日常使用可以把 CLI 安装成独立工具，不依赖当前源码目录：

```bash
uv tool install /absolute/path/to/tsm-agt
tsm-agt --version
```

进入要操作的工程后，首次启动按以下顺序：

```bash
tsm-agt init --workspace .
# 编辑新建的 .env，填写 endpoint、model 和 API key
tsm-agt doctor --workspace . --model-check
tsm-agt chat --workspace .
```

`init` 只创建私有 `.env` 模板：macOS/Linux 权限为 `0600`，已有文件绝不覆盖，也不会提前创建 `.agent` 运行数据。`init --print` 可只打印模板。Windows 使用独占创建保证不覆盖，但 ACL 需要在 Windows 原生环境复核。

`doctor` 检查 Python 3.12、当前平台、工作区、`.agent` 可写性、平台 Adapter 和三个模型配置；默认不联网、不运行项目代码。只有显式添加 `--model-check` 才发送一条固定且不包含项目数据的最小 Chat Completions 请求。`--json` 适合脚本/CI。报告永远不显示 API Key，只显示是否配置；endpoint 仅显示 scheme、host 和 port，不显示账号、路径、query 或 fragment。任一必需检查失败时退出码为 1。

`flow` 通过 SQLite 只读连接读取 `<workspace>/.agent/runtime.db` 中已经持久化的 Event Log，不会再次调用模型、执行工具或改变 Task/Event 记录；数据库不存在时也不会创建空文件。默认树形输出只包含节点、状态与事件序号，不展示 Goal/Prompt、工具参数、环境变量和日志正文。`--live` 只在 Event cursor 前进时刷新，任务到达终态后自动退出；尚未终结的任务可按 Ctrl+C 停止观看，这不会停止任务本身。`--live --json` 每行输出一个完整 JSON 投影，便于脚本逐条读取。

`--node` 接收流程树或 JSON 中的 `node_id`，显示类型、状态、耗时、父子节点、直接 Edge、Event 锚点、白名单事实、明确因果下游和失败归类。`--failure` 直接选择首个可行动失败；没有失败时明确报告，不猜节点。诊断只显示 token/错误码/风险/审批决定/exit code/输出字节数与截断标志/Mutation 前后哈希等结构化事实，不显示 Prompt、工具参数、日志正文、审批预览和自由文本 reason。当前协议能确定归类 F3/F4/F5/F8/F9/F11/F13，其他类别显示 `unclassified`；完整 Diff/Evidence、日志受控引用和其余分类仍待对应协议完成。

`--kind` 和 `--status` 可以重复使用。同一选项的多个值按“或”匹配，不同选项之间按“且”匹配；例如 `--kind tool --kind model --status FAILED` 表示失败的 Tool 或 Model。结果中的 `match` 是真正命中的节点，`context` 是为了看懂层级而保留的祖先。筛选不会修改原流程，也不会创造不存在的因果 Edge。`--node` 与筛选互斥。

`--timeline` 用稳定的 task/model/tool/process/user/control/workspace 泳道展示先后、状态和耗时；`--tui` 在终端中显示 Task 概览与时间线，配合 `--live` 原位刷新。它们仍是同一 Flow Projector 的只读视图，不会展示隐藏推理。

`--export` 把完整投影或筛选视图写成版本化的脱敏 JSON、逐行可解析的 JSONL 或无外网依赖的离线 HTML。目标必须位于工作区内、父目录已经存在；敏感路径和 `.agent` 被拒绝，已有文件不会覆盖。导出物只含结构化 Flow/Timeline，不含 Event payload、Prompt、工具参数、环境变量、日志正文或隐藏推理。`--export` 不与 `--live`、`--node`、`--json`、`--tui` 组合。macOS 已验证私有权限为 `0600`；Windows 使用平台 ACL Adapter，等待原生 CI 验证。

`replay --list` 只列出事件序号、类型和时间，`--at SEQ` 显示该事件发生后的历史 Flow；不指定定位方式时显示最新帧。`--turn` 可按 Turn ID 跳转，`--node` 可按流程中的 Tool、Approval、Process、Mutation 等节点 ID 跳转；`--boundary start|end` 选择开始或结束帧，默认结束。运行中节点没有真正的结束事件时会明确报错。回放前会校验完整 Event Log、Task 状态和终态事件，使用 SQLite 只读连接，不调用模型、工具或进程，也不会重放写文件、命令和网络等副作用。

`replay --play` 会按 Event 的历史时间间隔自动逐帧播放：`--speed 1x` 原速、`2x` 等待减半、`max` 不等待。`--from/--to` 选择闭区间；`--step` 每帧等待 Enter，q 只停止观看。`--save-cursor` 在每帧显示后原子保存最后看完的 Event，`--resume-cursor` 从下一帧继续；游标不能覆盖普通 JSON、跨 Task 使用或倒退。为防止异常时间戳或长时间空闲让终端卡住，单帧默认最多等待 2 秒；被压缩的长间隔标记 `gap-capped`，时间戳倒退标记 `timestamp-regressed`。`--play --json` 每行输出一个 snapshot+timing。播放只维护一个增量 Flow，不重放副作用。`--rerun` 仍待 Prompt Manifest、安全 fixture 和副作用重放协议完成。

`inspect --effective-config` 使用 SQLite 只读连接返回已经落盘的配置，不会读取“现在”的模型配置来伪造历史。输出包括模型公开标识、仅保留 origin 的 endpoint、凭据是否已配置及来源、Provider 能力、Tool 风险/幂等和参数 Schema hash、Adapter 版本/能力/健康、Policy/Trust/Sandbox、环境指纹、Prompt Manifest 与各类 hash。API Key、完整 endpoint 路径/参数、Prompt 正文、Tool Schema 正文和环境变量值不会保存或输出。当前实现 revision 1；Steering/config diff 自动生成后续 revision 仍待后续步骤。Extension 和 Workflow 仍以 `not_selected` 明确占位，不冒充已经实现。

Prompt 目前按固定顺序装配 `System Safety → Harness Instructions → Untrusted Project Onboarding（若有）→ Untrusted Project Memory（若有）→ Session Conversation Projection（若有）→ Task Working Memory（非空时）→ Conversation → Current User Input`。系统段以独立 `system` 消息发送，Onboarding/Memory/Session/Working Memory 使用明确边界的 `user` 数据消息，用户输入和 Tool Result 保持各自角色，不可信数据不能被拼接进系统指令。Manifest 保存段 ID、来源、版本、信任等级和正文 Hash，不保存正文；每次 `llm.completed`/`llm.failed` 只记录最终 Prompt Hash、Manifest Hash、角色顺序、消息数和 Toolset Hash。空白 Working Memory 不进入 Prompt，避免浪费小上下文窗口。由于尚未接入 Provider tokenizer，静态段 token 数明确标记为 unavailable，不使用字符数冒充 token。Trusted Project Instructions、Workflow、Skill 和精确 Provider tokenizer 属于后续步骤。

上下文预算/压缩已接入 Agent Loop：Provider 声明窗口时，默认在“估算输入 + 预留输出”达到窗口 80% 后触发；固定 Token 延迟门槛默认关闭，可通过 `TSM_AGT_CONTEXT_LATENCY_SOFT_TOKENS` 显式开启。低于阈值时，Session 不再按固定字符数提前裁掉 Task；每个 Task 都进入模型候选上下文。达到阈值后，统一压缩器保留全量 Task 最小索引（Task ID、目标、状态、产物路径、完成/剩余工作、确认事实、Outcome 和来源 Event），同时保留最近至少 8 条消息及完整 Tool Call/Tool Result 原子组，只压缩较早的对话详情与冗长执行记录。旧历史摘要带 `untrusted_historical_data` 边界，不让旧文件中的指令升级为 system 内容。`context.compacted` Event 与 Flow 节点显示压缩前后估算、消息数和 Hash，不保存摘要/旧正文；压缩后仍无法安全装入窗口时以 F7 明确失败。当前估算方法是保守的 UTF-8 字节启发式，不是精确 Provider tokenizer；完整旧正文的私有 Scratchpad 归档和按需历史检索仍待后续实现。

大型上下文即使没有接近 Provider 硬窗口，也可能拖慢模型，但默认不再用固定 `40k` 提前压缩。`TSM_AGT_CONTEXT_COMPACTION_RATIO` 默认 `0.80`；`TSM_AGT_CONTEXT_LATENCY_SOFT_TOKENS` 默认 `0`，表示只按窗口比例触发。如用户更重视延迟，可显式设置正整数作为性能软门槛。摘要保留工具名、查询、目标路径、候选文件和命中数量，省略源码正文、日志正文和命中行文本；后续模型仍知道“查过什么、哪些文件值得读”，不会只剩无意义 Hash。

搜索路线遵循“先宽后窄”：首次工作区搜索可以发现候选，出现大量命中后要求读取候选或缩小目录；只有真正读取源码文件才锁定源码范围，Markdown/日志等说明材料只作为线索，不会把 Agent 困在文档目录。锁定源码后无理由返回全仓搜索会被阻止。Monorepo 全仓搜索优先源码根和源码文件，并在多个同级源码根之间轮流扫描，避免 `codeBase`、`modules`、`packages` 中字母靠前的一份耗尽 5000 文件额度。已知多个独立文件时，提示模型在一个响应中提交多个只读调用，Runtime 按现有安全边界逐个执行。

活动 Agent Turn 现在会在模型调用和工具执行的安全边界保存版本化 Checkpoint。模型返回 Tool Call 时，`llm.completed` 与下一 Checkpoint 在同一个 SQLite 事务提交；工具执行前先保存待处理调用，工具结果回填后再保存消息轨迹，因此进程崩溃后 `resume <task_id>` 可以继续同一 Turn。恢复前校验 Checkpoint 完整性、工作区关键配置指纹、Effective Configuration、Prompt Manifest、Toolset、Provider capabilities、Policy 与 Adapter Lock，并先收敛 Workspace 事务和后台进程；不匹配时进入 `CONFLICT`，不会继续执行。等待审批的任务不能用 resume 绕过 approve/reject，非幂等 RUNNING 工具仍按 Tool Ledger 进入 `UNKNOWN_OUTCOME`，不会自动重试。

项目记忆已实现为可替换的 `ProjectMemoryPort`：模型可通过 `core.memory_list/remember/verify/forget` 显式管理带来源的稳定事实，不能静默把聊天或推测写成长久记忆。写入、验证和删除均为 R1 审批动作并接入 keyed-idempotency；项目身份变化会让对应事实变成 stale，默认不注入模型。有效记忆以 `untrusted_project_memory` 的 `user` 消息边界进入 Prompt，不能变成系统指令。SQLite 位于工作区 `.agent/memory.db`，Event/Flow 只保留 Hash 和审计元数据，不复制事实正文。

当前 Runtime 真实层级是 `Session → Task → Turn → Model/Tool Call`。每个 Task 只能属于一个 Session；未显式选择 Session 的旧入口会原子创建确定性的 standalone Session。`TASK` Memory 仅当前 Task 可见，`SESSION` Memory 按 `session_id` 在同一 Session 的多个 Task 之间共享；旧数据库中没有 `session_id` 的历史 `SESSION` 记录兼容为 TASK scope。`SessionContextProjector` 从 Session Event 确定性重建用户可见对话和全量 Task 最小索引；低于 Provider 压缩阈值时保留全部可见对话与 Task 摘要，达到阈值后再由统一上下文管理器缩减旧详情，但不删除 Task 身份、产物路径、关键状态与来源 Event。普通文本 Turn 和工具 Agent Turn 都写入同一投影，SQLite 重启后可重建等价上下文；不同 Session 隔离。Session 不共享原始 Tool Result、审批、后台进程、Tool Ledger、凭据或隐藏推理。

Working Memory/Scratchpad 1.0 通过 `core.working_memory_read/update` 让真实工程 Agent 维护当前 Goal、Constraints、Facts、Decisions、Hypotheses、Open Questions、简单 Plan、Completed/Remaining Work 和 Evidence 引用。它是 Task 临时工作台，不是长期 Memory，也不保存隐藏推理、凭据、原始日志或源码正文。更新使用完整快照、revision 乐观锁和 keyed idempotency；Evidence 只能引用真实 `event:`、`tool_call:` 或 `mutation:`。R0 内部状态更新仍经过 Policy 和 Tool Ledger，但不弹审批；白名单之外的 R0 写操作仍拒绝。Checkpoint 绑定 Working Memory Hash，并发改动会让恢复进入 `CONFLICT`。最终 Turn 将状态与会话结果一起写入 Session，后续 Task 可以继续未完成事项。可用 `tsm-agt working-memory show <task_id> [--json]` 检查；Flow/Replay 只展示 revision/hash，不泄漏正文。用于大日志和原始正文的 Private Artifact Scratchpad 尚未实现，不能与本能力混为一谈。

持久化 Session 可通过 `session create/list/show/close/archive/select-task/flow` 管理，使用 `task create --session <id>` 创建归属 Task。Session Event 有独立 cursor；`session flow` 只展示状态、Task 列表和事件类型，不替代原有单 Task `flow`/`replay`。Session 上下文 revision 会绑定 Agent Checkpoint。仅新增会话消息或切换模型、且没有待执行 Tool Call 时，恢复会刷新当前 Session 上下文并重新采样，不重放旧 Tool；Workspace、Policy、Toolset、Working Memory/Evidence 等执行身份变化仍进入冲突。SQLite、CLI 与纯 Python 合约已在 macOS 验证；实现不使用平台路径语法，Windows 原生回归暂未执行。

Project Onboarding 会在 Task 首次进入 `RESOLVING_PROJECT` 时进行有界静态扫描，识别语言/构建系统、源码边界、入口、候选构建测试命令和受信任规则文件。它不会运行 Wrapper、构建脚本、测试、依赖安装或项目代码；候选命令仍必须通过普通 Tool、Trust、Policy 和审批。五个阶段分别保存 Checkpoint，崩溃后可继续；未变化项目按规范化工作区和本地主体复用缓存，变化后生成新 revision。摘要带来源路径/Hash，以 `untrusted_project_onboarding` 的 `user` 数据段进入 Prompt；规则正文不会借 Onboarding 绕过 Trusted Project Instructions 门禁。

只读工具始终把输出标为不可信数据。工作区内路径可直接读取；模型确实需要访问工作区外的现有文件或目录时，Runtime 会暂停同一个 Agent Turn，向用户展示规范化目录、只读权限和当前 Task 范围。批准后，原 Tool Call 自动恢复，并且同一 Task 的 `core.read_file/list_files/find_files/search_text` 可以继续使用该目录；新 Task 不继承授权。磁盘根目录和用户 Home 不会作为可批准根目录。

额外目录授权不会交给写文件工具、命令工具或任意第三方 Tool Adapter，也不会改变主工作区。即使已经批准只读访问，逃逸授权目录的 symlink/Junction、Windows ADS、`.git`、`.ssh`、`.env*`、凭据文件和常见密钥/证书格式仍然拒绝。除目录浏览、正文读取和正文搜索外，`core.find_files` 可按已知文件名或相对路径通配模式直接定位文件；它跳过 build/dist/node_modules/target 等生成目录，避免模型明知文件名仍逐层 `list_files`。目录列表只证明“这里有这个文件”，若结论依赖资源名、配置键、常量或别名，Agent 还必须搜索并读取定义，不能把目录存在当成定义证据。

代码导航通过独立 `CodeIntelligencePort` 接入，真实 Engineering Composition 默认提供七个 R0 只读工具：`code.symbol_overview`、`code.definition`、`code.references`、`code.implementations`、`code.workspace_symbols`、`code.diagnostics` 和 `code.rename_preview`。内置 `TextCodeIntelligenceProvider` 是无需额外依赖的有界回退实现，支持 Python、Kotlin、Java、TypeScript/JavaScript、Go、Rust、Swift 和 C/C++ 常见声明；每个结果都带 Workspace fingerprint、index version、是否刷新、文件/语言覆盖与截断状态。源文件变化后索引自动刷新，未变化时复用内存索引。

内置回退的 definition/reference/implementation/rename 属于保守的词法导航，不冒充编译器级语义；Python diagnostics 当前只检查语法，其他语言明确不返回伪诊断。`rename_preview` 永远不会写文件，并明确 `safe_to_apply=false`，需要模型或用户检查后再通过正常写文件工具和审批协议应用。以后替换成 LSP、tree-sitter 或 Serena Adapter 时，Kernel、Agent Loop、CLI 和 Tool Schema 无需改变；不注册 Code Intelligence 时，基础文件工具仍可独立工作。

## 使用真实模型

真实 Agent 只读取三个显式环境变量，不会自动复用其他应用的 API Key。可以在终端中 `export`，也可以复制 `.env.example` 为项目根目录的 `.env`；已导出的变量优先于 `.env`：

```bash
export TSM_AGT_MODEL_BASE_URL="https://api.example.com/v1"
export TSM_AGT_MODEL="your-model-name"
export TSM_AGT_MODEL_API_KEY="your-api-key"
# GPT-5 类接口若拒绝 max_tokens，则改用 max_completion_tokens。
export TSM_AGT_MODEL_OUTPUT_TOKEN_PARAMETER="max_tokens"
# 兼容网关若拒绝 function.strict=true，可设为 false；Kernel 仍会本地校验参数。
export TSM_AGT_MODEL_STRICT_TOOL_SCHEMA="true"
export TSM_AGT_MODEL_STREAMING="true"

uv run tsm-agt agent \
  "分析这个项目的入口文件" --workspace .
```

本地 `.env` 已被 Git 忽略。三个必填模型字段是 Base URL、模型名和 API Key；此外可显式配置超时、重试、输出 Token 参数名和严格工具 Schema。预算加载器只接受下文列出的 `TSM_AGT_AGENT_*` 和 `TSM_AGT_EXPLORATION_*` 字段，其他变量不会被注入进程环境。
导出的环境变量优先于 `<workspace>/.env`。`agent`、`chat`、`resume`、`approve/reject` 和 `answer` 都以 `--workspace` 定位同一份配置和 `.agent/runtime.db`；配置缺失时会给出 `init`、编辑位置和 `doctor --model-check` 的可执行修复步骤，不再只输出变量名。

调查工具没有写死为 12 次。默认软上限为 24 次，Agent 仍可能因为连续无新证据、累计工具耗时或需要给最终回答预留额度而提前收尾；Agent Loop 另有 40 次工具调用硬上限，负责阻止失控循环。可以在工作区 `.env` 中按项目覆盖软预算，导出的同名环境变量优先：

```bash
TSM_AGT_AGENT_MAX_MODEL_CALLS=20
TSM_AGT_AGENT_MAX_TOOL_CALLS=50
TSM_AGT_AGENT_FINALIZATION_MODEL_CALLS=2
TSM_AGT_AGENT_EXECUTION_RESERVE_MODEL_CALLS=1
TSM_AGT_AGENT_RECOVERY_RESERVE_MODEL_CALLS=1
TSM_AGT_AGENT_VERIFICATION_RESERVE_MODEL_CALLS=1
TSM_AGT_EXPLORATION_PROFILE=balanced
TSM_AGT_EXPLORATION_MAX_TOOL_CALLS=30
TSM_AGT_EXPLORATION_MAX_ACTIONS=30
TSM_AGT_EXPLORATION_MAX_TOOL_SECONDS=180
TSM_AGT_EXPLORATION_LOW_VALUE_STREAK=3
TSM_AGT_EXPLORATION_RESERVE_TOOL_CALLS=2
TSM_AGT_EXPLORATION_MIN_ACTIONS=2
```

`TSM_AGT_EXPLORATION_PROFILE=balanced` 是默认值：调查按 Evidence Question、
语义目标和已有证据关系推进，不再把刚读文件的父目录当成硬范围。无明确关系的
只读动作最多得到一次缩小结果量的探测，仍然必须通过 Workspace、Sandbox、
Approval 和 Tool Risk。`legacy` 只用于兼容旧目录策略；未知 Profile 会让启动明确
失败，不会偷偷退回宽松策略。Profile 和具体 Adapter ID/版本会进入 Effective
Configuration，活动 Turn 恢复时仍由配置 Hash 防止无痕换策略。

这些值用于新 Turn；活动 Turn 已把调用上限保存在 Checkpoint，配置变化不会在执行途中偷偷改额度，恢复校验发现规则变化时会进入冲突并要求重新开始。`AGENT_MAX_*` 是防止失控循环的最后硬上限；`FINALIZATION`、`EXECUTION`、`RECOVERY`、`VERIFICATION` 四类预留分别保护最终回答、首次实施、失败恢复和实施后验证，默认是 2/1/1/1。它们不会机械地给每个阶段各执行一次模型请求，而是在存在对应未完成 Outcome 时阻止宽泛探索提前吃光后续额度。若本 Turn 的硬额度确实用完，但工作仍可恢复，Task 会保存 Checkpoint 并进入“未完成、可继续”，不会误报 `BLOCKED`；下一次继续沿同一 Task 增加新一轮额度，不重放已完成工具。`EXPLORATION_MAX_TOOL_CALLS` 不能大于 Agent 工具硬上限。`EXPLORATION_MAX_TOOL_CALLS` 统计本 Turn 中全部已用工具，`EXPLORATION_MAX_ACTIONS` 统计被策略评分的调查动作，二者用于不同的保护维度。

CLI 的实时进度会明确写成“本次 Task”，并显示当前用户目标、要确认的完整问题、实际工具及参数、路径/查询范围、范围为何扩大，以及四类探索计数：动作、工具调用、累计工具耗时、连续低收益。开始收尾时不再笼统显示“预算即将耗尽”，而是显示真实触发项及“已用/上限”。这些限制只约束当前 Task，不是一整个 Session 的累计上限；Session 中下一个 Task 会得到自己的一份新预算。

常见诊断：

```bash
# 只检查本地安装与配置，不访问模型
tsm-agt doctor --workspace .

# 检查真实 Provider 协议与凭据
tsm-agt doctor --workspace . --model-check

# 输出脱敏 JSON
tsm-agt doctor --workspace . --json
```

## 交互式使用

配置模型后，可以直接启动持久化工程对话：

```bash
uv run tsm-agt chat --workspace .
```

启动后会打印 `session_id`。每条普通输入创建一个隔离的 Task，并自动完成 Intake、项目发现、Agent Loop、验证和收尾；同一 Session 的后续 Task 会获得前面用户输入和最终回复的带来源引用，但不会继承原始 Tool Result、审批权、额外目录授权或后台进程。模型请求写文件、执行受控命令等动作时，CLI 会展示 risk、action、target、preview、网络、数据传输和回滚信息，只有输入 `y/yes/approve` 才批准；其他回答默认拒绝。模型请求读取工作区外的关联项目时，CLI 会单独显示“需要额外目录访问权限”、目录、只读范围和仍然禁止的能力；批准只恢复该次只读调用，不等于批准写入或命令执行。

模型给出最终回答后，CLI 还会输出可信验证结果，例如 `verification: passed (2 criteria)`。Flow 中会出现 `Verification → workspace-integrity / post-mutation-command` 节点；节点下钻只显示状态、Evidence 数量和是否全部通过，不复制测试日志正文。当前内置命令识别覆盖 Python unittest/pytest/compileall、Gradle/Maven、Node 包管理器 test/build/lint/check、Cargo、Go、Flutter/Dart、Xcode、常见编译器、CMake/Make/Ninja；普通 `git status` 或打印命令不能充当验证证据。

当缺失信息会实质改变结果、权限或不可逆动作时，模型可调用 `core.request_input` 暂停当前 Task。CLI 会显示一个短问题和最多三个选项；回答后继续原来的 Turn，不会新建 Task。问题暂时不回答时，CLI 会给出带一次性恢复令牌的命令，令牌默认 24 小时有效：

```bash
uv run tsm-agt answer <request_id> \
  --token <resume_token> --answer "dark" --workspace .
```

普通信息不足时模型应采用可见假设继续，不能把 `request_input` 当成频繁反问工具。Clarification 的问题和答案正文不会进入 Flow/导出诊断字段；流程只展示等待、解决状态、选项数量和内容 Hash。

交互命令包括 `/help`、`/session`、`/tasks`、`/resume`、`/flow` 和 `/exit`。退出、EOF 或在输入提示处按 Ctrl+C 都会保留 Session；之后可继续：

```bash
uv run tsm-agt chat \
  --session <session_id> --workspace .
```

如果 Session 中存在未完成 Task，任何普通输入都会进入统一的语义决策流程：可替换 `SessionInputResolverPort` 结合用户原文、近期对话和挂起 Task 摘要判断这是新任务、恢复某个任务还是需要澄清。默认 Adapter 使用当前模型理解任意语言和历史指代；Core 与 CLI 没有“继续/重试”关键词表。Resolver 只能提出动作，Runtime 仍独立校验 Task 归属、Checkpoint、工作区、策略、工具、Working Memory/Evidence 和副作用安全。自然语言恢复时，本轮用户原文完整加入原 Task；不会被 Runtime 拆句或替换。`/resume` 查看挂起目录，`/resume <task_id>` 明确恢复，`/new <goal>` 明确创建新 Task。已经开始但结果不明的非幂等工具、`UNKNOWN_OUTCOME` 或身份冲突不会自动重放；审批和澄清也不能被普通文本绕过。

Agent 正在执行时，第二段普通输入默认使用 `AUTO`：可替换 `RuntimeInputClassifierPort` 结合当前目标和 Task 状态判断这是补充当前任务、替换目标、排到当前任务之后，还是仅查询状态；默认生产 Adapter 复用当前模型，Core/CLI 不含中英文关键词表。Runtime 再独立执行置信度、审批、澄清和副作用安全检查。`/followup steer|queue` 可让用户明确固定后续输入模式，`/followup auto` 恢复语义判断，`/followup` 查看当前模式。显式 `/steer <补充要求>`、`/queue <后续任务>`、`/redirect <新目标>` 是确定性人工覆盖；`/after` 和 `/replace` 是兼容旧名称。队列保存在 SQLite，`/queued` 可查看，`/unqueue <input_id>` 可取消。Redirect 保留已完成工具结果和已提交修改，只取消尚未执行的工具调用。

如果在审批提示输入 `leave` 或 Ctrl+C，审批保持待处理且不授予权限，可再使用现有 `approve/reject` 命令处理。在 `answer>` 直接回车、EOF 或 Ctrl+C，则 Clarification 保持待处理，可使用上面的 `answer` 命令恢复。当前 REPL 支持模型流式输出、采样期间 Ctrl+C 快速取消，以及等待模型首个响应时每 5 秒一次的进度心跳。交互式终端使用精确锁定的 `prompt-toolkit==3.0.52` 处理 Unicode 显示宽度、中文 IME 提交、方向键、退格/Delete、历史和粘贴；管道/重定向输入仍使用普通 stdin。

普通 Agent 与文本回合默认最多输出 `8192` tokens；Session 路由、运行中输入分类等内部短调用继续使用各自显式的小预算。

## Python SDK 与本地 Event API

外部 Python 程序通过同一个 Runtime 门面创建、等待、恢复、审批、中断和订阅任务；SDK 不复制 Agent Loop：

```python
import asyncio
from pathlib import Path
from tsm_agt.sdk import EngineeringAgentClient

async def main():
    async with EngineeringAgentClient(Path("/absolute/project")) as client:
        accepted = await client.submit_task(
            "分析失败测试", command_id="my-app-request-001"
        )
        async for event in client.subscribe_events(
            accepted.task_id, after=0, timeout=30
        ):
            print(event.cursor, event.event_type)
        async for item in client.subscribe_progress(
            accepted.task_id, after=0, timeout=30
        ):
            # 本机认证 UI 的实时明文：goal/question/path/query/tool arguments
            print(item.sequence, item.progress.to_data())
        result = await client.wait_task(accepted.task_id)
        print(result.state, result.assistant_text)

asyncio.run(main())
```

所有写命令都要求调用方提供稳定 `command_id`。相同 ID、相同参数重试返回既有 Task/结果；相同 ID 换参数会拒绝，避免网络重试重复批准或重复中断。SDK 关闭时会先把正在执行的 Task 安全落成 `INTERRUPTED`，再取消本地后台协程。

需要接 UI 或其他本地进程时，可启动回环 HTTP/SSE Adapter：

```bash
tsm-agt api serve --workspace . --host 127.0.0.1 --port 8765
```

启动时会生成并只显示一次 Bearer Token。服务拒绝 `0.0.0.0` 或局域网地址，所有路由都必须携带 `Authorization: Bearer <token>`。主要接口：

- `POST /v1/tasks`：提交 `{goal, command_id}`。
- `GET /v1/tasks/<task_id>`：查询状态、待审批/追问和最终结果。
- `GET /v1/tasks/<task_id>/events?after=<cursor>&wait=30`：SSE 断线续传。
- `GET /v1/tasks/<task_id>/progress?after=<sequence>&wait=30`：本进程实时进度 SSE。
- `POST /v1/tasks/<task_id>/interrupt`：安全中断。
- `POST /v1/approvals/<request_id>`：批准或拒绝。
- `POST /v1/clarifications/<request_id>`：回答追问。

`events` 是可持久化审计通道，只返回 `event_id/task_id/cursor/type/time/schema_version`，不返回原始 Event payload、Prompt、工具参数、环境变量、日志或隐藏推理。客户端以 `event_id` 去重，并在处理成功后保存最后确认的 cursor；断线后将它作为 `after` 继续即可。

`progress` 是只存在于当前 Runtime 进程内的实时 UI 通道。它通过同一个 Bearer Token 保护，直接返回完整 Goal、Evidence Question、工具名与参数、路径/查询范围、预算明细和收尾原因，不做内容脱敏，也不写 SQLite；Runtime 重启后旧的实时明文不会恢复。Web UI 应在任务执行时订阅 `progress` 显示操作内容，并使用 `events`/Flow 做可恢复历史与审计，不能再从脱敏 Event 猜当前问题或范围。两条通道都不提供模型隐藏思维链。

`TSM_AGT_MODEL_BASE_URL` 应填写 `/v1` 根地址，Adapter 会请求其 `/chat/completions`。目前使用 Chat Completions 的函数工具协议；Provider 必须支持 `tools`、`tool_calls` 和 JSON Schema 参数。内部工具名如 `core.read_file` 会在 Adapter 内映射成兼容的函数名，模型结果进入 Kernel 前再还原。

不同 OpenAI-compatible 网关可能只在可选字段上有差异：GPT-5 类模型常要求 `TSM_AGT_MODEL_OUTPUT_TOKEN_PARAMETER=max_completion_tokens`；部分网关收到 `function.strict=true` 会直接空断流，此时配置 `TSM_AGT_MODEL_STRICT_TOOL_SCHEMA=false`。关闭 strict 只是不向模型声明严格模式，工具调用进入 Kernel 后仍必须通过本地参数 Schema、Policy、Approval、Sandbox 和 Workspace Boundary 校验。

如果网关可以正常返回非流式 JSON，但 SSE 响应总是在缺少 `finish_reason` 的情况下结束，可在私有 `.env` 设置 `TSM_AGT_MODEL_STREAMING=false`。Agent 的工具调用和多轮循环保持不变，只是不再逐字显示模型输出。默认值为 `true`。

## 依赖方向

```text
core ──────→ ports ←────── adapters
                   ↖
adapter registry ───┘

bootstrap/composition.py 负责选择并组装具体 Adapter。
```

- `core` 和 `ports` 不得导入 `adapters`、`extensions` 或厂商 SDK。
- Adapter 之间不得相互导入。
- 具体 Adapter 只能在 `bootstrap/composition.py` 中实例化。
- Runtime Adapter 只接收最小 `AdapterContext`，不能获得 Runtime 或数据库对象。

## 术语

| 名称 | 本项目中的含义 |
|---|---|
| Port | 核心定义的能力接口，例如 `ModelProviderPort` |
| Runtime Adapter | Port 的具体代码实现，例如 Echo Model 或 SQLite Store |
| Adapter Registry | 登记 Adapter、按 Port 解析实现并管理生命周期 |
| Extension Package | 未来面向用户安装的一整包能力，可包含 Adapter、Skill、Workflow 和配置 |

`Runtime Adapter` 不等于 Codex 设置里的 Plugin。Codex Plugin 是用户安装的能力包，更接近本项目未来的 `Extension Package`。
