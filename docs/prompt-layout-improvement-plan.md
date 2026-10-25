# 提示词与上下文编排实施方案

状态：改动一、二已实施，详见第七节；改动三待 Review。修订为面向所有主会话任务的通用方案：按职责整理完整决策规则、调整最新执行上下文的位置、建立请求装配诊断与分类对照评测。Todo 为可选组成，不是主线或验收标准。

## 一、实施边界

- 不增加完成分类器、结束拦截、额外复核模型或强制 Todo 清空机制。
- CompletionReview 保持关闭；文本答复结束循环的协议不变。
- 第一版不引入 developer 消息角色，不机械迁移 Codex 的角色：当前多协议实现只有 system/user/assistant/tool。
- 保留用户最新要求的优先性；Todo 仅为模型维护的执行记录，不是扩大授权的依据。
- 不重写历史存档、不删除旧 assistant 答复、不把历史承诺改写成执行事实。
- 原生工具调用和对应结果保持配对、次序与标识；不修改供应商要求回传的 reasoning/signature 数据。
- 规则整理不删减必要分支，不把概括性主线当作完整提示词，也不把任务处理改成 Runtime 强制执行的固定七步工作流。
- 普通问答可以直接结束；需要取证、实施或等待结果时才进入相应分支。验证不专指执行测试，进度不专指 Todo。

## 二、补充考虑的场景与处理

| 场景 | 处理 |
| --- | --- |
| 用户只要分析、计划、Review | 清单可保留未完成项；模型根据当前请求交付，不因剩余项自动实施 |
| 用户改范围、暂停、取消 | 当前输入及运行状态优先；旧 Todo 标记为范围关系未判定，不自动重新执行 |
| 旧计划属于上一轮 | 带 task/turn/revision 和来源，不能把全部历史项当成本轮交付合同 |
| 审批挂起、恢复、预算耗尽 | 沿用既有循环边界；恢复前重新构造状态，不在等待审批时多调模型 |
| 一批多个工具，含失败与成功 | 等循环允许下一次模型调用时才构造状态；不把提示插进调用和结果之间 |
| 工具返回成功但进程仍运行 | 显示 running/unknown；命令启动收据不是验证通过 |
| 验证退出码为 0 | 仅陈述该检查终止状态；不能推断全部目标正确或完成 |
| 编译通过但逻辑未覆盖 | 记录检查类型和证据，模型判断覆盖范围；不自动把 Todo 置完成 |
| 浏览器等非命令验证 | 保留相关工具证据；命令识别器无记录不等于没有验证 |
| 验证后再次修改 | 保留修改与检查的顺序和已有哈希；提示旧证据可能不覆盖新改动，不一概要求全项目重跑 |
| 没有真实服务、凭据 | 仅陈述受阻检查；不代替模型判定独立编译、导入和局部测试不可用 |
| 没有 Todo 或新会话 | 不制造计划；无状态时不产生无意义的尾部消息 |
| 提示注入内容进入 Todo、文件名、工具消息 | 模型写的任务文本与外部文本始终是数据；不拼入 system 指令作为权威要求 |
| 上下文压缩、重启恢复 | 状态从最新持久化事实重建，仅保留一份当前快照；压缩不能删掉来源和不确定性 |
| 极小上下文或超长清单 | 有明确预算；优先当前项、计数、证据引用，省略时标注省略量；装不下按既有机制报预算不足 |
| Chat Completions/Responses/Anthropic | 分别检查最终序列化布局；不依赖尾部 system 在所有供应商保持原位置 |
| 工具集变化、只读模式、Skill 加载 | 以本次实际暴露的能力为准，不把历史可用工具当成当前可调用工具；Skill 与项目要求继续遵守来源和范围 |
| 路由、规划、完成复核等其他模型调用 | 尾部执行状态只用于 Conversation Loop 的 AGENT_TURN，不给其他调用附加实施要求 |
| 自定义提示词、用户约束与旧配置 | 保留显式覆盖和来源版本，不能为“强化”覆盖用户限制；新轮升级与审批恢复分开处理 |

## 三、改动一：按职责整理完整的通用决策规则

### 修改文件

- `src/tsm_agt/core/prompt_templates/base.md`
- `src/tsm_agt/core/prompt_templates/communication.md`
- `src/tsm_agt/core/prompt_templates/coordination.md`
- `src/tsm_agt/core/conversation_loop/prompt.py`
- `src/tsm_agt/core/prompt.py`

### 内容组织

1. `request-pipeline-guidance` 统一拥有目标理解、授权延续、推进与交付规则，并引用其他段落负责的分支；不能把它简化为一句“坚持完成”。
2. `delivery-mode-guidance` 仅拥有工程实施与适度验证规则，继续按写工具能力启用。
3. `tool-feedback-guidance` 仅拥有失败恢复、结果不确定与重复副作用规则。
4. `task-plan-guidance` 只描述读取、状态更新、完成条件和版本维护。
5. `completion-behavior-examples` 保留少量简短示例，覆盖直接回答、仅交付分析、实施完整效果、可恢复失败；不写 MCP 等业务特例。多步骤示例可以无 Todo，避免模型误学为“只有清单任务才持续执行”。
6. 动态提醒改为简短解释状态的文字，不再复制全部“必须实施、必须验证”段落。

以下是需要在实际提示词中保留的决策与责任，不是由 Runtime 按固定顺序强制调度的阶段：

| 决策 | 必须保留的分支和约束 | 负责段落 |
| --- | --- | --- |
| 理解目标与范围 | 确认交付物、历史约束、当前范围变化、授权延续；问题本身已清楚时不重新请求确认 | request-pipeline-guidance |
| 判断证据是否足够 | 可直接回答的问题不强制用工具；未见过的源码、外部材料或环境事实按需取证；历史说法不等于当前事实 | request-pipeline-guidance，workspace/external 工具段落 |
| 选择下一项行动 | 根据当前实际可用能力、必要依赖和副作用决定行动；合理细节自主解决，必要决策/审批才等待用户 | request-pipeline-guidance，具体工具段落 |
| 执行与观察 | 区分未开始、已启动、终止与未知结果；处理部分、截断或不完整证据；只对观察到的事实作陈述 | tool-feedback-guidance，具体工具段落 |
| 恢复与调整 | 修正可恢复失败，不盲目重试未知副作用；独立工作继续；正在运行的进程先跟进结果而非重复启动 | tool-feedback-guidance |
| 检查交付物 | 问答/研究检查结论与证据，代码按影响范围选择检查，文档检查内容/渲染；检查结果只覆盖自身范围 | request-pipeline-guidance，delivery-mode-guidance |
| 判断继续或结束 | 仍有可行的授权工作则继续；目标已达成可交付；暂停、取消、必要输入、实际阻塞或预算边界如实结束/挂起 | request-pipeline-guidance，既有 Runtime 边界 |

记录进度是横向职责，不是每个任务必经步骤。用到 Todo 才同步清单；复杂工作可以维护 Working Memory，无清单也需要完成用户要求的多项工作。

概括性主线仅用于文档解释。代码中保留上述分支及相应段落，不新增请求类型关键词路由器，不让 Runtime 自行判断用户需求已经完成。

保留“说明未验证不代替可行的验证”，也保留“不对文档修改强制运行项目”的例外。不能为求简短丢掉审批边界、未知副作用检查、部分结果覆盖范围等现有约束。

模板加载器会合并 Markdown 软换行，few-shot 必须用实际装配后仍明确的段落/标签边界。示例留在固定提示词中，不伪造原生工具调用、不加入历史执行证据。

### 验收

- 使用能力相同的工具集对比，静态提示词估算长度不增长；若某项必要约束增加，说明替换了哪段重复文字。
- 无写工具时不出现要求写文件的规则；计划/分析任务不会被强制转为实施。
- 对照职责矩阵逐项检查：去掉的是重复表述，不能遗漏部分结果、真实阻塞、未知副作用、用户改范围、比例验证等分支。
- 简单问答不制造计划；多步骤实施即使没有 Todo，也应按用户请求完成，不以清单存在与否决定持续执行。
- 更新段落版本及 manifest；只对稳定哈希做快照，不再大量复制整段提示词作为测试预期。

## 四、改动二：把通用的最新执行上下文放到下一次决策附近

### 修改文件

- 新增 `src/tsm_agt/core/conversation_loop/execution_context.py`：生成通用状态视图，复用现有事实投影；Todo、修改和检查信息都可缺省，不另建 Todo 存储。
- `src/tsm_agt/core/conversation_loop/loop.py::_prepare_model_call`：统一采集这次请求使用的状态，压缩前后使用同一份视图。
- `src/tsm_agt/core/conversation_loop/prompt.py`：把动态事实与可信说明分开。
- `src/tsm_agt/core/prompt.py::assemble`：通过同一布局声明组织消息。
- `src/tsm_agt/core/context.py`：预算估算、压缩保护、生成消息去重。
- `src/tsm_agt/core/kernel_memory.py`：保留完整 Working Memory 视图与语义，给末端视图提供精简数据。

布局结构可共享，但尾部执行状态只在 Conversation Loop 主模型调用启用。`PromptTemplate` 仍被其他调用路径使用；无此状态的路径保持原有语义。路由、规划和复核模型不会因此被要求修改文件或维护 Todo。

### 推荐逻辑顺序

```text
稳定 system 规则（含工具能力说明与 few-shot）
项目约定、环境与可信运行说明
项目发现信息、必要历史与来源标记
当前真实用户输入
本轮完整 assistant/tool 交换
最新执行上下文（user 角色、明确为数据；无新增有效状态时省略）
→ 下一次模型响应
```

“最新执行状态”是请求投影，不是一条新的真实用户指令，不写入会话对话存档，不改变 source user 的定位。将其前缀加入所有生成上下文识别/清理/压缩保护逻辑；采用统一分类函数，避免各调用点分别维护不同前缀列表。

第一版将已有执行事实与必要 Working Memory 投影集中成一份最新状态视图。可包含最近工具结果的完成状态、仍在运行的操作、必要证据引用；存在计划时附清单，存在修改时附修改/检查事实。没有清单、修改或验证命令的任务也能使用它，但不因此制造这些对象。

不在头尾各放一份完整清单。其他事实、假设与证据按预算保留。优先采用引用或摘要，不能先重复注入再依赖压缩清理。不重述用户原句，不捏造“当前用户意图”“已授权”或“整个目标完成”的运行时标签。

### 状态内容

```json
{
  "kind": "current_execution_state",
  "task_id": "...",
  "turn_id": "...",
  "working_memory_revision": 4,
  "scope_relation_to_current_request": "not_assessed",
  "plan_record": {
    "active_item_id": "step-2",
    "open_count": 6,
    "items": [
      {"id": "step-2", "status": "in_progress", "content": "示例任务记录"}
    ]
  },
  "action_observations": {
    "latest_mutation_ref": "...",
    "recognized_check_status": "missing",
    "evidence_refs": []
  }
}
```

此示例是带计划和修改的情况，不是所有请求的必填模板。无计划时省略 `plan_record` 及对应范围关系；无修改时省略修改后检查状态。这里的 `missing` 仅表示现有识别器未记录到匹配检查，不是“全部验证未做”的判断；`items` 在实现中为受长度限制的数组。没有相应来源的字段不填写或明确为 unknown。

数据视图包含来源版本、必要事件序号和可用的修改哈希。未知来源保持未知；不新增一个模型填写的“已授权”布尔值。可信 system 规则只解释如何使用该数据，不把任务文字、路径或工具原文升级为 system 指令。

### 生命周期与兼容性

- 每次请求清除上一份生成视图，从已提交事实重新投影；不积累每轮提醒。
- 只在现有模型调用边界生成，不改变工具批次调度或审批/取消行为。
- 压缩使用与最终装配相同的快照及布局，不能在估算后额外追加未计费内容。
- 恢复 checkpoint 后重新生成，不把旧 checkpoint 内的摘要当作最新事实。
- 同一次装配若已有多个持久化投影读取，统一版本或复用同一事件范围，避免 Todo 和动作状态来自不同时间点。
- 概念状态栏第一版预算建议不超过约 800 估算 token；任务正文按当前项、剩余项顺序取用，明确展示省略项数。预算不能删除真实用户输入和未闭合工具协议。
- 不移动、不丢弃 Responses 的 reasoning item 或 Anthropic 的签名块；工具组保持原样。
- Anthropic 可能合并相邻 user 内容，按最终内容块确认状态在工具结果之后；可信规则仍可合入供应商顶层 system。

## 五、改动三：统一装配诊断并做对照评测

### 装配诊断

`src/tsm_agt/core/prompt.py` 中新增明确的布局对象（例如 `PromptLayout`），同时驱动 `manifest()` 与 `assemble()`，区分“配置布局”和“实际启用后的布局”。配置 manifest 可以包含未启用的条件段落，实际 receipt 必须只记录真实输出。

扩展 `PromptAssemblyReceipt`：每项包含 ordinal、source/segment id、role、trust、字符数、content hash、估算 token 与估算方法；记录布局版本与当前状态投影版本。不记录正文、凭据、原始日志、图片数据或工具实参。

新增供应商序列化后的无正文布局描述，建议放在 `src/tsm_agt/ports/model.py` 的诊断类型及 `src/tsm_agt/adapters/request_layout.py` 的共享实现：

- `adapters/openai_compatible/model.py::_build_payload`：在协议说明插入和工具名编码后描述实际 messages/tools。
- `adapters/openai_compatible/responses.py::_responses_payload`：描述转换后的 input item 类型和顺序。
- `adapters/anthropic_messages/model.py`：描述顶层 system 与 messages/content blocks 的实际顺序。
- `core/kernel_model.py`：附带不含正文的供应商布局元数据；每次模型请求均能对应到模型调用号，不能只观察最终答复。

避免通过共享的“最近请求”字段关联，防止并发会话或重试串台。使用这次请求的不可变摘要，记录 model-call/provider-attempt/transport-stage。布局摘要与现有显式开启的敏感请求捕获分开，不能默认打开原始请求捕获。

同时计算供应商新增格式说明、工具描述扩展的预算差；没有 tokenizer 时明确是估算，不能把消息 ordinal 当作准确 token offset。

### 测试位置

- `tests/test_prompt_manifest.py`：布局来源唯一、条件段落、版本、来源角色；验证相对次序和约束，不把某一轮固定消息条数作为通用断言。
- `tests/test_context_compaction.py`、`tests/test_todo_context.py`、`tests/test_continuation_context.py`：最新版本唯一、压缩保护、SQLite 恢复、无清单、旧清单范围未判定。
- `tests/test_conversation_loop.py`、`tests/test_approval_context_continuity.py`：多工具混合结果、审批挂起恢复、取消、预算边界。
- `tests/test_runtime_action_facts.py`、`tests/test_tool_result_model_projection.py`：运行中进程、检查覆盖范围、修改后旧检查、非命令验证证据；不以工具 ok 或零退出码判定用户目标已完成。
- `tests/test_openai_compatible_model.py`、`tests/test_openai_responses_model.py`、`tests/test_anthropic_messages_model.py`：实际序列化角色/块次序、协议前置、thinking/reasoning/signature 保留、工具配对不变。
- `tests/test_multiturn_evaluation.py`：空操作模型不能计为成功，正文和密钥不会进入普通结果报告。

### 真实模型评测

扩展 `scripts/evaluate_multiturn_behavior.py`，新增合成场景文件 `scripts/fixtures/prompt-layout-comparison.json`。使用临时工作区及独立 Runtime；复用既有工具、配置和评测逻辑，不发输入到用户真实会话。

四组：A 当前基线；B 仅合并规则；C 仅调整状态位置；D 两者组合。诊断增强对四组一致启用。位置对照保持状态数据内容一致，防止把内容变化误算为位置效果。

评测覆盖通用分支，不以所有任务都用工具、运行测试、清空 Todo 为通过标准：

| 样例类别 | 独立验收依据 |
| --- | --- |
| 简单问答 | 正确回答；不强制制造计划、调用工具或修改工作区 |
| 源码分析、资料比较 | 查到必要证据，结论有支持；没有未经请求的写入 |
| 无 Todo 的多文件实施 | 完成所有指定效果，适当验证；与有 Todo 的实施使用同等完成要求 |
| 有 Todo 的实施与后续继续 | 效果完成，清单状态与已完成项一致；不把写清单当实现 |
| 文档与配置修改 | 内容/格式或项目配置效果满足要求；验证与风险相称 |
| 可恢复错误、部分/截断结果 | 补齐必要证据或采用可行方法，避免盲目重复 |
| 缺少真实服务但可做本地检查 | 完成可行工作和局部检查，清楚界定未覆盖的部分 |
| 进程仍在运行 | 查询/等待已有操作，不重复启动，不把启动收据当成功结果 |
| 用户改变范围、只要计划、暂停取消 | 尊重新请求，保留实际状态，不被旧清单推动越界执行 |
| 长历史、错误历史声明、压缩与恢复 | 保留必要限制和最新事实，不继承错误完成结论或旧授权判断 |

固定模型/供应商、推理强度、工具集与顺序、预算和初始文件；记录供应商实际返回模型标识及时间。重复运行并交错执行组别，避免一次成功即归因。合成数据对外模型调用沿用脚本的显式开关和费用边界，不回放私有真实请求。

分开计分：独立验收通过率、提前停止率、适用任务的检查尝试与终止结果、有 Todo 样例的状态一致性、分析任务误写率、模型/工具调用数、token 与耗时。不适用的指标记 N/A，不作为失败或成功混入分母。评测器独立运行验收通过不等于模型自己做过验证；不靠最终答复措辞作为唯一通过依据。

先小样本确认方向，再扩大；不预先宣称任何提示词变体保证完成。若 D 没有改善或明显增加误执行/成本，保留诊断能力并回退布局，重新查模型与协议差异。

## 六、实施顺序与交付

1. 先统一布局声明和无正文诊断，锁定当前基线和协议不变量。
2. 依据通用决策职责矩阵整理规则，提供修改前后约束覆盖、长度与版本对照。
3. 实施精简状态视图和尾部位置，补全压缩/恢复/协议测试。
4. 运行四组真实模型评测；未运行或运行受阻必须如实标注。
5. 根据证据选择默认布局；保留旧布局用于回退。回退不需要修改历史事件、Todo 或 checkpoint schema。

保持服务中当前任务不被重启打断。提示词/布局变化在新轮次加载；等待审批的工具恢复必须继续遵守原批准作用域，不能因新的提示词或工具变化自动重放副作用。

## 七、本次落地记录（2026-10-24）

按本次授权，仅实施改动一、二。改动三的统一布局诊断、供应商请求诊断扩展及四组真实模型评测仍待实施；本文前面的总体顺序是原方案，不代表已执行。

- 基线已提交并推送：`b44193f`，提交信息 `skill功能支持`。
- 通用推进/结束判断集中到 `base.md` 的 request 段；工程验证、工具恢复、Todo 维护各自负责，不增加 Runtime 完成判定。
- 同一工具集下，全部静态段正文由 20,777 降至 17,157 UTF-8 字节（约 -17.4%）；无工具时由 7,884 降至 6,499（约 -17.6%）。这是正文长度对照，不是供应商 tokenizer 的精确 token 数。
- 新增 `context_messages.py` 共享生成上下文前缀；主循环、源用户定位和 checkpoint 刷新同步识别尾部状态。
- 新增 `execution_context.py`，每次主循环调用在预算计算前生成 `current_execution_state`。工具交换之后以 user 数据消息发送，system 中只保留短说明；无计划/动作时不生成。
- 实际字段为 `plan_record`（revision、source_content_hash、source_event_sequences、items、open_count、omitted_count）、`recent_actions`、可选 `recognized_post_mutation_check`。沿用既有识别器，明确 `recognized_commands_only`；不推断用户意图、授权或整体完成。
- 正文预算为 3,200 UTF-8 字节。清单优先进行中、待办、其他项，最多 8 项并限长；记录省略数。完整清单仍在 Working Memory；头部保留其他事实与证据，只移走 plan/plan_state。
- 从本轮持久化执行记录补回压缩掉的动作事实，不把合成工具调用/结果插进实际对话。其他轮次动作不带入本轮检查判断。
- 统一 `PromptLayout` 留给改动三；本次直接扩展现有 assemble，并让预算使用同一份投影消息。没有改 checkpoint schema、历史事件或持久化 Todo。
- 回归覆盖长度限制、刷新去重、来源角色、无 Todo、非命令检查、运行中状态、压缩和 SQLite 恢复、审批连续性，以及 Chat Completions / Responses / Anthropic 的工具结果与尾部顺序。Responses 的已有 reasoning 数据保留；未扩展 Anthropic 适配器原有的 thinking/signature 支持能力。

提示词和结构回归通过不能证明真实模型必然持续执行、验证或更新 Todo；真实行为改善仍需改动三的对照评测。CompletionReview 配置未修改。本次没有重启正在运行的服务，避免中断已有工作；服务重新加载代码后生效。

本次验证：13 个相关测试文件，157 passed / 6 skipped；`git diff --check` 通过。全量测试遇到范围外失败及长时间停滞，已停止；其中架构检查的 4 项失败在独立提取的 `b44193f` 上复现，因此不报告全量测试通过。本次实现改动保留在工作区，尚未另行提交。
