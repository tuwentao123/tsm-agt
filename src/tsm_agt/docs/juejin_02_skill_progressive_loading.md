# Skill 目录太长怎么办：先保留名称，再缩短说明，最后才搜索

Skill 可以理解为一份给 Agent 使用的工作指南。例如，“接口检查”Skill 会说明先看什么代码、检查哪些兼容性问题、怎样验证结果，具体步骤保存在 `SKILL.md` 中。

安装很多 Skill 之后，模型需要先知道“有哪些、分别能做什么”，才能选择要读的指南。这份名称和用途清单，就是本文说的 **Skill 目录**。

本文解决一个具体问题：**目录太长时，怎样控制发送量，同时尽量让模型看见所有 Skill 的名称，减少为了找一个名称就多查一次目录的情况。**

## 一、当前 Skill 从发现到使用的完整流程

先区分目录和正文：

```text
目录：review-api —— 检查接口兼容性
正文：review-api/SKILL.md 中的详细步骤
```

Agent 在后台发现可用的指令型 Skill，并保留完整目录。给模型组装请求时，再根据所选模型的窗口大小生成简短目录。模型选择后，通过 `skill.load` 读取正文；名称或用途不明确时，可以先用 `skill.discover` 查询。

```text
Agent 发现可用 Skill，建立完整后台目录
  → 根据所选模型窗口，确定目录预算
  → 生成模型可见目录
      名称和完整用途都能放下：直接展示
      说明放不下：保留名称，均匀缩短说明
      连名称都放不下：展示部分名称，提示可查询
  → 模型选择 Skill，必要时先用 skill.discover 查询
  → skill.load 读取 SKILL.md，必要时继续读取参考文件
  → 模型按正文中的方法，使用普通工具完成任务
```

| 内容 | 什么时候给模型 |
| --- | --- |
| 名称和用途 | 建立上下文窗口时，按预算生成目录 |
| 完整目录项，包括路径、来源和覆盖关系 | 调用 `skill.discover` 时，按查询和分页返回 |
| SKILL.md 正文及参考文件 | 调用 `skill.load` 时读取 |
| 目录变化 | 追加目录更新，告知模型当前可用能力 |

## 二、为什么优先保留名称

目录需要同时控制长度和保留查找线索。全部用途说明一直增长，会挤占任务和工具结果的空间；简单地逐条添加到预算用完，又可能让前面的长说明占掉空间，导致后面的 Skill 连名称都看不见。

例如 `alpha`、`beta` 的说明很长，如果因此省略了后面的 `review-api`，模型处理接口检查任务时，就少了一条可直接使用的线索。

提供查询工具可以补充信息，但不能保证模型一定主动查找自己不知道的能力。因此，目录优先给全部名称留位置，再分配用途说明的空间：

```text
alpha：短说明
beta：短说明
review-api：检查接口兼容性
```

预算更紧时，只要全部名称还能放下，就先保留名称。不过，名称不清楚时，模型仍可能需要查完整用途，不能保证完全消除查询。

## 三、预算怎样计算，怎样配置

预算在 Agent 给当前模型组装请求时计算。后台仍保留完整目录，不会因为某个模型的窗口较小就一起裁掉。

```text
已知模型窗口：预算 = 模型窗口 × 0.02，向下取整，至少为 1
未知模型窗口：预算 = 2,048 个估算 token
```

窗口为 32,000 token，默认目录预算为 640；窗口为 128,000 token，默认预算为 2,560。窗口未知，指模型能力配置没有提供有效的正数窗口大小。

在 `config.yaml` 中可以调整：

```yaml
context:
  skill_catalog_ratio: 0.02
  skill_catalog_fallback_tokens: 2048
```

比例必须大于 0、小于 1；备用预算必须是正整数。**2% 和 2,048 是可调整的初始策略，没有实测证据说明它们适合所有项目。**

对应代码：

```python
def tokens_for(self, context_window: int) -> int:
    return (
        max(1, int(context_window * self.ratio))
        if context_window > 0
        else self.fallback_tokens
    )
```

这里使用本地估算：把目录标题和完整目录 JSON 序列化后，按 UTF-8 字节数除以 4、向上取整。它不是模型厂商的分词结果，中英文和特殊字符都可能有估算误差。

预算包括目录外层字段，但不包括工具定义、消息包装和历史记录；整个请求仍由上下文管理器检查总预算。

## 四、放不下时，具体怎么处理

### 1. 初始目录只保留名称和用途

后台条目还包含路径、来源和覆盖关系。模型初次选择时，先看到：

```json
{
  "name": "review-api",
  "description": "检查接口兼容性，并执行相关验证"
}
```

完整信息仍可通过 `skill.discover` 查询；正文和相应来源信息通过 `skill.load` 读取。

目录外层还提供版本、总数、是否有条目未展示、说明是否缩短，以及查询工具名称。模型因此能区分“全部名称都在，只是说明短了”和“还有名称没有展示”。

### 2. 先保住名称，再给说明分配空间

完整说明放不下时，程序先检查：只放全部名称能否容纳。

能容纳时，给所有说明使用同一个字符上限，再通过二分查找，找到能放下的最长公共上限。例如，预算允许每条最多保留 18 个字符，就所有条目都按这个上限处理；原本更短的说明完整保留。

这个规则限制的是字符前缀，不代表每条占用相同的 token。每次尝试都会重新检查整个目录的估算量。

核心逻辑可以概括为：

```python
minimum = catalog(shortened_entries(0), shortened=True)
if skill_catalog_estimated_tokens(minimum) <= budget_tokens:
    low, high = 0, max_description_length
    while low < high:
        middle = (low + high + 1) // 2
        candidate = catalog(shortened_entries(middle), shortened=True)
        if skill_catalog_estimated_tokens(candidate) <= budget_tokens:
            low = middle
        else:
            high = middle - 1
    return catalog(shortened_entries(low), shortened=True)
```

上面省略了变量准备，完整实现见 [skills.py](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/skills.py)。

如果所有条目连一个字符的说明都加不进去，就保留全部名称，标记 `descriptions_shortened: true`。这里采用文字前缀裁剪，不额外调用模型生成摘要，因此 Skill 说明应把关键用途写在前面。

### 3. 连名称都放不下，才省略条目

这时按名称排序，逐条尝试放入名称，并设置以下提示字段：

```json
{
  "total_skills": 1000,
  "has_more": true,
  "discovery_tool": "skill.discover"
}
```

某个名称自身过长时，程序跳过它后继续尝试后面的名称，避免一个异常条目挡住所有后续条目。后台目录仍完整，省略的 Skill 可以查询和读取。

极小的预算如果连目录外层字段都装不下，就不发送这条目录消息；查询和读取工具的定义仍保留。

## 五、一次使用要经过哪些步骤，会多一次模型交互吗

假设用户说：“帮我检查接口兼容性。”

如果目录已能说明 `review-api` 的用途，模型可以直接调用 `skill.load`：

```json
{
  "name": "review-api"
}
```

Agent 读取 `SKILL.md`，把正文作为工具结果返回，再请求模型继续工作。

如果只看到了名称、看不懂用途，或者目标名称没有展示，模型可以先调用 `skill.discover`：

```json
{
  "query": "接口",
  "offset": 0,
  "limit": 5
}
```

查询匹配名称或完整用途说明。选好之后，再用 `skill.load` 读取。结果有更多页时，通过 `next_offset` 继续；默认每页 20 条，上限 50 条。

当前查询是文字子串匹配，没有语义搜索。例如说明只有“API”，查询“接口”可能找不到，需要调整查询词。

如果用户已经明确点名 `review-api`，模型也可以直接读取，即使名称没有出现在初始目录中，也不要求先查询。程序始终根据完整后台目录检查名称。

```text
用户请求 → 模型看目录或用户点名
           ├─ 名称和用途明确：skill.load
           └─ 不明确：skill.discover → skill.load
         → 必要时读取正文引用的参考文件
         → 使用普通工具完成实际任务
```

**查询不是固定前置步骤。需要查询时，确实会增加一次工具往返和一次后续模型请求。** 名称优先的目录分配方式有助于减少查找名称的需求，但不能保证模型永远不用查询。

读取 Skill 只加载工作方法，不会自动运行测试或获得额外权限。

## 六、目录变化，会不会影响 Prompt Cache

预算按模型窗口计算，**不按“这轮还剩多少上下文”重新计算**。相同配置、相同目录得到相同结果，历史变长不会让目录反复换长度。

会话窗口建立后，初始目录保留在原位置。后续新增 Skill、名称或用途变化时，通过追加目录更新告知模型；不会回头改写初始目录。正文、查询结果同样作为工具结果追加到历史。

模型或预算配置变化、上下文压缩等情况，会建立新的窗口基线。仅目录修订号变化不会强制重建窗口。

这些措施减少不必要的前缀变化，但不保证 API 一定命中缓存，实际缓存读取量仍以接口返回为准。

`skill.load` 支持可选的 `catalog_revision` 检查：携带的目录版本已过期时返回 `SKILL_CATALOG_CHANGED`；不带版本时读取当时的文件。版本对应整个目录。已经进入历史的旧正文不会自动更新，需要时重新读取。

名称不存在或目录版本过期时，错误结果只返回当前目录版本、总数和查询入口，不附带整份目录。模型需要了解详情时，再按需查询。

## 七、当前目录的大小、覆盖范围和使用成本

这套机制通过只展示名称和用途、按预算缩短说明，控制初始目录的占用；同时尽量保留名称，让模型知道有哪些能力可以使用。完整说明和正文按需读取，错误结果也不会一次性附带整份目录。

目录变短也有成本：说明缩得太短，模型可能需要额外查询；查询结果和正文进入历史后，仍会占用后续输入。**目录预算只能控制初始目录大小，不能直接代表整个任务节省了多少 token。** 任务费用和耗时还取决于查询次数、历史增长、缓存命中和任务是否成功。

可以使用项目中的离线评估脚本查看目录占用。在项目根目录执行：

```bash
.venv/bin/python scripts/evaluate_skill_catalog.py --context-window 128000
```

下表列出当前机制的测量结果。样本为合成 Skill：名称为 `demo-skill-0000` 这样的固定格式，每条有较长中文说明。模型窗口设为 128,000，目录预算为其 2%，即 2,560 个估算 token。

| Skill 总数 | 可见名称数 | 目录估算 token | 说明情况 |
| --- | --- | --- | --- |
| 3 | 3 | 577 | 完整用途 |
| 20 | 20 | 2,560 | 缩短用途 |
| 100 | 100 | 2,510 | 缩短用途 |
| 300 | 300 | 2,260 | 仅名称 |
| 1,000 | 341 | 2,558 | 仅名称，其余可查询 |

例如 300 条时，所有名称都能进入目录，但完整用途需要查询；1,000 条时，连全部名称也放不下，需要通过查询获取未展示的条目。名称数量和说明完整度应一起看，不能仅凭目录 token 数判断模型是否容易找到目标。

这里只计算目录文本，未包含完整请求、模型输出和后续结果；没有发送真实模型请求，也未测量延迟、缓存命中或自动选择 Skill 的准确率。名称、说明和窗口大小不同，结果都会变化。

## 八、代码与验证

| 代码 | 负责什么 |
| --- | --- |
| [skills.py](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/skills.py) | 预算策略、目录估算、保留名称和缩短说明 |
| [skill_tools.py](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/adapters/builtin/skill_tools.py) | 完整目录快照、查询、读取和精简错误提示 |
| [loop.py](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/core/conversation_loop/loop.py) | 根据所选模型窗口生成请求中的简短目录 |
| [context_configuration.py](https://github.com/tuwentao123/tsm-agt/blob/main/src/tsm_agt/bootstrap/context_configuration.py) | YAML 配置读取和参数校验 |

`tests/test_skill_catalog_budget.py` 覆盖中英文和特殊字符的预算边界、保留全部名称、极端目录省略提示、模型窗口差异、分页查询和正文读取。

请求级测试还检查：已知名称可直接读取，没有被程序强制增加查询；读取结果返回后的下一次请求保留前一次请求的消息前缀。脚本化模型验证的是程序行为，不能代表真实模型一定会主动选对 Skill。

运行相关测试：

```bash
.venv/bin/python -m pytest tests/test_skill_catalog_budget.py tests/test_bundled_skills.py tests/test_skill_loading_errors.py tests/test_user_skills.py -q
``

本文讨论以 `SKILL.md` 提供步骤的指令型 Skill；代码型 Skill 仍作为可执行工具提供，不纳入这套目录压缩规则。
