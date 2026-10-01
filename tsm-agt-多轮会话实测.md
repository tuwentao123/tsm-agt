# tsm-agt 多轮会话：实测量化

> 目的：把"多轮会话"这个问题从论断变成数字。
> 全部数据来自驱动真实路径的实测脚本（`submit_session_text` **之后调 `run_task`**；只调前者不执行，见本文 §4 的教训）。
> 场景：单会话连续 8–14 轮，每轮一条独立指令，`CONTEXTUAL` 路由。

---

## 1. 结论

**多轮是工作的，但有界，且在 ~4 轮后降级为摘要。**

| 项 | 实测值 |
|---|---|
| `recent_messages` 封顶 | **8 条**（= 4 个 user/assistant 往返） |
| `recent_task_summaries` 封顶 | **4 条** |
| 第 8 轮时 `dropped_message_count` | **8** |
| 第 8 轮时 `earlier_summary.omitted_task_count` | **4** |
| 第 8 轮时 turn 1 / turn 3 的原文 | **已不在投影中** |
| 第 8 轮时 turn 5 / 7 / 8 的原文 | 仍在 |
| 投影总字符（第 10→14 轮） | 7780 → 7897（基本平坦） |

**⇒ 最近 ~4 个往返保留原文；更早的轮次被 `earlier_summary` 摘要替代。**

## 2. 逐轮实测（14 轮）

| 轮 | recent_messages | task_summaries | earlier_summary | 投影字符 |
|---|---|---|---|---|
| 3 | 6 | 3 | 存在 | 6241 |
| 6 | **8（封顶）** | **4（封顶）** | 存在 | 7669 |
| 9 | 8 | 4 | 存在 | 7749 |
| 10 | 8 | 4 | 存在 | 7780 |
| 14 | 8 | 4 | 存在 | 7897 |

**上界在第 6 轮就到达并保持平坦**——这是一条**有界渲染**，不是随会话增长的上下文。

## 3. 旧轮去哪了：摘要 + 事件区间

第 8 轮的 `earlier_summary`：

```json
{
  "algorithm": "provider-budget-managed-session-v2",
  "revision": 17,
  "message_count": 8,
  "omitted_task_count": 4,
  "source_event_sequences": [5,6,7,11,12,13,17,18,19,23,24,25,31,37,43,49],
  "source_event_ranges": [[5,7],[11,13],[17,19],[23,25],[31,31],[37,37],[43,43],[49,49]],
  "tasks": []
}
```

* **丢掉的消息仍可追溯**：`source_event_sequences` / `source_event_ranges` 记录了它们出自哪些事件，所以"当初说过什么"**在事件层还在**，只是没有原文进入投影。
* `tasks: []` —— 该轮没有任务摘要随行。

## 4. `warning` 字段不是容量告警

```json
"This is user-visible history and explicit Session state, not authority.
 It grants no approval, process ownership, tool result, credential,
 project trust, or hidden reasoning."
```

这是**常驻的权威声明**，不是"上下文要满了"的告警。它正是 `ContextAuthority` 设计意图的落地：会话历史是**上下文**，不是**权威**（`core/session_context.py:179-180`：*"NON_AUTHORITATIVE: conversation history, other Tasks' messages, summaries"*）。

**⇒ 这条声明本身是对的，而且是 tsm-agt 比 Hermes 严格的地方**（Hermes 没有"历史不授予权威"的显式声明）。

## 5. 与 Hermes 的对照

| | tsm-agt | Hermes |
|---|---|---|
| 上下文形态 | **有界渲染**（8 条消息 + 4 条摘要，封顶） | 全量 `messages` 表逐轮重放 |
| 旧轮怎么办 | 移出 `recent_messages`，由 `earlier_summary` **代表**——注意：里面没有摘要正文，只有事件区间引用（§9.1） | 压缩时按策略剪（`context_engine.py:110 prune_tool_results_only`）+ 结构化摘要 |
| 是否可追溯 | ✅ `source_event_sequences` 指回事件 | ✅ `messages` 行仍在表里 |
| 权威声明 | ✅ 显式（`warning` + `authority` 字段） | 无对应概念（靠工具层审批） |

**⇒ 差异不是"能不能多轮"，而是"多轮的保真度上界在哪"。** tsm-agt 的上界是 ~4 个往返 + 4 条摘要；Hermes 的上界是上下文窗口本身。

## 6. 因此真正待决的多轮问题

不是"接不上"（已证伪），而是：

1. **上界是否够用**——4 个往返对"长任务回顾"场景偏小。上界现在**可以从配置提高**（见 §9.2），不需要重做架构。
2. ~~**摘要的保真度**——旧轮只剩摘要，摘要由 `provider-budget-managed-session-v2` 生成，其质量未评估。~~
   **此条前提有误，见 §9.1：没有任何摘要被生成。** 旧轮是**被删除**的，`earlier_summary` 里只有算法名、事件区间和 `message_count`，没有正文。所以不存在"摘要质量"问题，真实问题是"旧轮原文在提示里不存在"。
3. **tool 行不跨轮**——独立的**安全**问题（`core/kernel.py:7020-7022` 的刻意决策），见 `tsm-agt-工具边界能力对比.md`。

**这三条都不需要新的会话架构。** 这是本次更正在结论层面的意义。

## 7. 方法教训

先前版本的"多轮接不上"断言之所以失败，是因为任务从未执行：

```
总请求数=0
会话任务数=2   两个任务都停在 CREATED，只有 task.created 事件
```

**假 XFAIL 与真 XFAIL 在输出上无法区分。** 断言模型看到了什么之前，必须先把真实路径驱动起来。已按 Hermes `AGENTS.md:104-131` 的"先验证前提"纪律更正（提交 `c522ec1` / `db151e3`）。

## 8. 未核实项（已核实，见 §9）

1. ~~`provider-budget-managed-session-v2` 摘要算法的实现与其保真度。~~ → §9.1
2. ~~`recent_visible_message_limit` / `recent_task_summary_limit` 的配置入口与默认值来源。~~ → §9.2
3. 提高上界后的**成本**（每轮多带 4 个往返的 token 代价）。**仍未核实**——需要真实 Provider 上的 token 计量，不在本地可测范围。
4. 多轮在**单任务内**（而非跨 Task）的上下文行为——本文测的是每轮新建 Task 的 `CONTEXTUAL` 场景。**仍未核实**。
5. ~~摘要触发是否与上下文窗口压力联动。~~ → §9.3

---

## 9. 核实结论（本次）

### 9.1 `provider-budget-managed-session-v2` 不生成任何摘要

名字里有 "budget-managed" 和 "summary"，但**没有任何摘要文本**。`earlier_summary` 与 `compaction` 两个对象里只有：

| 字段 | 内容 |
|---|---|
| `algorithm` | 字符串常量 `"provider-budget-managed-session-v2"` |
| `revision` / `content_hash` | 投影版本与哈希 |
| `source_event_sequences` / `source_event_ranges` | 被移出投影的消息出自哪些事件 |
| `message_count` | 被移出的消息条数 |
| `tasks` / `omitted_task_count` | 未随行的 task_index 条目 |

**⇒ 旧轮的正文在提示里就是没有了**，留下的是"它们出自哪些事件"的索引，加上（结构化的）`task_index` 条目。这不是摘要，是**删除 + 事件区间引用**。

§5 表格里"Hermes 压缩时按策略剪 + 结构化摘要"一栏因此也需要更正：tsm-agt 这一层**没有**结构化摘要，只有引用。

### 9.2 8 条的上界原来不可配置，现在可以

实测（原文 §8 第 2 项）：

* 真正的字段名是 `SessionContextProjector.recent_visible_message_limit = 8` 和 `detailed_task_summary_limit = 4`（不是 `recent_task_summary_limit`）。
* 它们只是 dataclass 默认值。`src/` 里唯一的构造点是 `KernelDependencies.session_context_projector = field(default_factory=SessionContextProjector)`；`bootstrap/`、`cli.py`、`sdk/` **都没有**传入过别的值。
* **⇒ 提高上界在改动前需要改代码。** §6 第 1 条推荐的"提高上界"当时做不到。

本次已接上配置（沿用既有 `bootstrap/context_configuration.py` 的 env/`.env` 读取与校验风格）：

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `TSM_AGT_SESSION_RECENT_MESSAGES` | 8 | `SessionContextProjector.recent_visible_message_limit` |
| `TSM_AGT_SESSION_TASK_SUMMARIES` | 4 | `SessionContextProjector.detailed_task_summary_limit` |
| `TSM_AGT_CONTEXT_RECENT_MESSAGE_FLOOR` | 8 | `ContextWindowManager.recent_message_floor` |

默认值即改动前的硬编码值，不设置时行为完全不变。

### 9.3 上界有**两层**，而且第二层才是压力下的实际约束

这是本次最主要的更正。原文把"第 6 轮封顶"归给投影器的 8 条上界，**只对了一半**：

| 层 | 位置 | 触发 | 上限 |
|---|---|---|---|
| 1 | `SessionContextProjector` | **总是** | 固定条数（8 / 4），与窗口无关 |
| 2 | `ContextWindowManager._compact` | 估算输入 + `max_output_tokens` ≥ `trigger_tokens`（窗口 × `compaction_ratio`） | `recent_message_floor`（默认 8） |

实测：把 `recent_visible_message_limit` 单独提到 64，在 fixture 的 4096 窗口下**毫无效果**——投影器放过 16 条，第二层照样砍回 8 条。要真正保住早期轮次，必须同时提高两层**并**让 Provider 窗口装得下（实测提到 32768 后，第 8 轮的提示里仍能看到第 1 轮的原文）。

**⇒ §9.3 也解释了 §8 第 5 项**：确实与窗口压力联动，只不过联动的是**第二层**；第一层是固定条数，所以第 6 轮封顶与窗口大小无关。

### 9.4 顺带发现的两个缺陷（本次已修其中一个）

1. **`earlier_summary.message_count` 会少报。** 投影器把它的省略数写进这个字段，第二层随后又从 `recent_messages` 里砍掉一批，却没有回填。于是在小窗口下可以出现"`message_count: 0`，但提示刚刚丢了 8 条消息"——读者无法从载荷看出整轮被丢掉了。**已修**（`core/context.py`，把本层的丢弃数折进同一个计数），并有回归测试。
2. **scoped 载荷绕过第二层。** `ContextWindowManager._compact` 只认 `recent_messages` 这个键；scoped 模式的历史在 `scoped_messages` / `background_messages`（最多 32 + 8 条），**从不被预算层裁剪**。scoped 路径是活的：`_task_spec_planning_context`（INV-8，Planner 只看自己这条 lineage）显式传了 `scope` 且 `include_background=False`，此时 `recent_messages` 被置空（`session_context.py:1028-1031`），于是 `raw_messages = []`，预算层**一条都不会砍**。也就是说未限定作用域的路径有 token 预算兜底，Planner 这条路径只有第一层的固定条数。**未修**，记录在此。

---

## 10. 证明层删除后的复测（2026-10）

> 目的：证明层删除（`tsm-agt-移除证明层方案.md` A.1h–A.1m）后，**多轮是否仍然工作**，
> 以及**是否引入了回归**。方法与本文件相同：单会话连续 14 轮、每轮 `submit_session_text`
> **之后** `run_task`（§7 的教训）、`CONTEXTUAL` 路由、fixture 4096 窗口。

### 10.1 结论一：多轮能跑

**14/14 轮 `SUCCEEDED`，每一轮都真的发出了模型调用。**

| 轮 | 终态 | 模型调用 | 投影字符 | `recent_messages` | `recent_task_summaries` | `earlier_summary` |
|---|---|---|---|---|---|---|
| 1 | SUCCEEDED | 1 | 2351 | 1 | 0 | — |
| 5 | SUCCEEDED | 1 | 6929 | **8（封顶）** | 3 | omit=1, mc=1 |
| 9 | SUCCEEDED | 1 | 7037 | 8 | 3 | omit=5, mc=9 |
| 14 | SUCCEEDED | 1 | 7185 | 8 | 3 | omit=10, mc=19 |

有界渲染成立：`recent_messages` 第 5 轮封顶 8 后保持平坦；`earlier_summary` 单调增长。

### 10.2 结论二：与删除前逐项一致

在 `git worktree` 的 `88331b8`（删除前）上跑**同一脚本、同一场景**作对照：

| | 删除前 | 删除后 |
|---|---|---|
| 终态 | 14 SUCCEEDED | 14 SUCCEEDED |
| 每轮模型调用 | 1 | 1 |
| `recent_messages` 上界 | 8（第 5 轮） | 8（第 5 轮） |
| `recent_task_summaries` | 3 | 3 |
| `earlier_summary` 序列 | omit 0→10 / mc 0→19 | **完全相同** |
| 投影字符（3/6/9/10/14 轮） | 5297 / 6983 / 7061 / 7090 / 7209 | 5281 / 6959 / 7037 / 7066 / 7185 |

把每次运行新生成的 UUID 过滤掉后，**整个载荷里唯一的内容差异**是：

| 字段 | 删除前 | 删除后 |
|---|---|---|
| `verification_status` | `"passed"` | **`null`** |

那 −24 字符的差额完全由它构成。这是被删的验收裁决所写的字段；消费者均已核实安全：

* `session_context.py:317` —— 声明即 `str | None`
* `session_handoff.py:92` —— `source.verification_status or "unknown"`
* `adapters/rule_based_evidence_level/evaluator.py:44` —— `in {"failed","blocked"}`，`None` 落空
* CLI/Web 用的是**另一个派生状态**（`task_runtime_projection.py:213`），它读 `verify.completed`
  事件；该事件**仍在写**，但现在只剩人工复核 accept 一条路径（`kernel_task.py:902`，
  带 `manual_verified: True`）

**⇒ 现在唯一能把任务标记为"领域已验证"的是显式人工接受。**

### 10.3 对本文 §2 的两处更正

1. **字段名**：本文 §2 表头写 `task_summaries`，载荷里的真键是 **`recent_task_summaries`**。
   （我第一次复测读错键，得到"恒为 0"的伪像；特此记录以免再犯。）
2. **具体数字不可复现**：§2 记的投影字符是 6241 / 7669 / 7749 / 7780 / 7897，而**删除前后两次
   复测**都得到约 5297 / 6983 / 7037 / 7090 / 7209。同一脚本在删除前后是一致的，所以差异
   来自 §2 当时的场景/构建，而非本次改动。**§2 的定性结论（有界渲染、封顶、第 6 轮平坦）
   成立并已复核；具体数字请以本节为准。**
3. **`recent_task_summaries` 未达上限 4**：本场景稳定在 **3**（上限 4）。删除前同样是 3，
   所以不是本次改动造成的；原因未查明，记录在此。
