# turn-bea45e 补丁冲突排查

本次未发现补丁匹配器误判。首次补丁引用的旧上下文与实际文件不同；模型读取对应范围后，重新提交已经成功。

## 证据

目标是 `src/tsm_agt/bootstrap/composition.py`。

| 事件序号 | 实际行为 |
| --- | --- |
| 8751 | `read_file` 返回第 1–191 行，`truncated=true`，总计 1478 行；提示从第 192 行继续。 |
| 8849 | 提交第一次 V4A 补丁，其中使用了第 205 行附近尚未读到的上下文。 |
| 8861 | 补丁第 23 行旧上下文不匹配，返回 `PATCH_CONFLICT`；没有任何编辑写入。 |
| 8885 | 模型补读第 192–251 行，看到了实际错误提示文本。 |
| 8920 | 使用相同文件版本哈希重新提交修正后的补丁，两个修改 hunk 均精确匹配，写入成功。 |

第一次补丁引用：

```python
"invalid TSM_AGT_ENABLE_MCP_TOOLS value: expected one of "
```

文件第 205 行实际内容：

```python
"TSM_AGT_ENABLE_MCP_TOOLS must be one of '', '1', 'true', 'yes', 'on', "
```

这是文字内容差异；两次补丁的 `expected_hash`、两次读取的 `sha256` 与修改前备份哈希均为：
`8352e109c1e76b3662e78efc7f73c9e41839ab2ebb186184c595adec1a2d8f0b`。
因而本例没有发生读取后文件版本变化，也不是拿错文件哈希。

## 重放验证

读取这次成功修改保存的原始备份，在内存中分别运行两份补丁，没有重新修改实际文件或会话：

- 首次补丁重现同样的冲突，完整诊断字段与历史结果一致。
- 修正补丁生成的内容 SHA-256 为 `bb1ff7d50307e5d3649900244a51644f8547eee1ab587643dd1472dde0fbc746`，与实际提交的 `after_hash` 一致，匹配位置与策略也一致。

记录见 [只读重放结果](evaluations/patch-conflict-bea45e/replay.json)。没有修改补丁工具、提示词或用户正在执行的文件，也没有重启服务。

## 影响与边界

补丁冲突增加了一次读取和一次重新提交；该改动最终成功，并没有一直卡在 `PATCH_CONFLICT`。
补丁成功只证明文本写入。之后 `python -m py_compile ...` 在启动前报 `EXECUTABLE_NOT_FOUND`，本轮结束前未观察到成功验证，不能据此确认整项 MCP 功能已经验证完成。

现有工具说明及编辑提示词已要求读取每个修改范围，且明确完整文件哈希不代表未返回的行已读；本例是模型没有遵守这条规则，随后按冲突诊断完成了重新读取和重打补丁。

## 为什么模型会生成错误的旧上下文

进一步检查审批事件保存的完整 agent checkpoint：第一次补丁前，`composition.py` 的工具结果仍明确保留第 1–191 行和继续读取提示。错误文字没有出现在这些文本消息中，首次出现在模型生成的补丁参数里。
因此可确定的是模型跳过了续读，在缺少目标范围原文时生成旧上下文；不是工具把已读到的第 205 行改成了另一句，也不能把它归因于压缩丢失了本轮已经读取的第 205 行——本轮此前根本没有读取那一行。
模型内部为什么忽略该指令无法从日志证实；“按常见代码样式补全”是符合证据的解释，不是可直接观察的推理记录。

现有规则要求忠实引用，但只是行为指令。哈希验证只证明文件版本一致，不证明模型看过文件的每一行，也不证明它生成的上下文来自该文件；正文匹配仍需单独校验。本次正是正文匹配阻止了错误补丁。

## 为什么服务能运行，工具却找不到 python

只读检查实际 Web 进程（PID 11342）：

- 服务命令以项目 `.venv/bin/python` 的绝对路径启动 Uvicorn。
- 服务的 PATH 中没有项目 `.venv/bin`。
- 用服务 PATH 查找 `python`：没有结果；查找 `python3`：`/opt/homebrew/bin/python3`。
- 项目 `.venv/bin/python` 存在、可执行，实际运行 `--version` 返回 `Python 3.12.13`。

该轮调用的 argv[0] 是裸名称 `python`。`kernel_process.py` 的 `_prepare_process_execution()` 对裸名称执行 `shutil.which(executable, path=safe_environment.get("PATH"))`；进程环境继承服务 PATH，没有自动加入项目虚拟环境，因此失败于 `resolve_executable`，还没开始编译。
这条路径不通过交互式 shell，不能依赖终端的 alias 或激活状态。使用虚拟环境解释器的绝对路径启动服务本身，不会自动改变其子命令的 PATH；现有 `scripts/restart.sh` 也是显式指定服务解释器，而非激活环境。

项目验证应选择已经确认的 `.venv/bin/python` 入口，不能用系统 `python3` 的存在推断它具备同一套项目依赖。
本轮模型在得到找不到裸 `python` 的结果后，没有继续查找项目入口或重试，这也是未完成验证的直接原因。checkpoint 中的 runtime environment 只包含工作区和路径语义，没有提供已发现的 Python 验证入口。

实际进程解析记录见 [服务环境检查](evaluations/patch-conflict-bea45e/process-environment.json)。本次分析没有修改 PATH、命令解析策略或项目运行文件。
