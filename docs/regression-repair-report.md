# 回归失败修复记录（2026-10-24）

## 实际修复

- 默认 Skill 工具注入遵守模型的 tools 能力。Fixture 组装新增 `enable_skills=False`，避免普通测试读取真实用户目录、安装收据和发现目录；Skill 测试显式启用并使用临时目录。真实支持工具的应用继续启用 Skills。
- Skill 文件和准备收据的文件持久化归入同一适配器；Skill 工具依赖 `ports/skill_files.py` 中的结构接口。Web 对存储和诊断实现的选择交给 bootstrap 组装入口。
- 将 SDK 重复的可继续状态集合归入 TaskState 的 `accepts_fresh_continuation`，没有扩大终态引用计数基线。
- 探索协调器与关系提供器共用 `core/exploration_identity.py`：修复一个保存 SHA-256、一个匹配原始 JSON 导致已观察调用不能再次匹配的问题。回归从真实 after_result 的状态再进入 before_call，不再手工伪造旧问题标记。
- SDK 的 runner 完成回调允许在 close 后安全到达，不再访问已释放的 application。

## 测试迁移原则

- 不恢复已删除的 Legacy/SINGLE_LOOP 模式、EvidenceQuestion 调用注解或强制完成复核。
- 旧的工具清单、配置来源、每轮完成状态和 SSE 字段断言调整为当前契约。普通会话答复后为 AWAITING_INPUT，预算耗尽保留未完成边界和 checkpoint，不伪报任务成功。
- 测试普通对话连续性或工具事实时给模型足够的上下文空间；专门的压缩/超限测试仍声明小窗口。Fixture 模型的 16K 窗口不修改真实模型的能力或生产预算。
- 依据仍存在的公共行为或底层策略验证审批、读取、实际工具结果、事件持久化和恢复。已不再自动运行的 Planner 准则降级逻辑改为直接测试该校验函数。
- 本地端口和 PTY 测试使用本机权限执行，避免把沙箱不允许 bind/PTY 当作产品失败；没有用新增 skip 或 xfail 隐藏失败。

改动保留在工作区；本次没有重启现有服务或重新开启 CompletionReview。

## 搜索顺序修复

启用 ripgrep 后，原始匹配输出的并行遍历顺序可能让文档先占满小结果上限，与 Python 回退搜索的顺序不一致。现在在截断/分页前稳定排序匹配文件，沿用源码根优先、同级根轮流取结果、同一文件行保持原序的规则。新增显式乱序 ripgrep stub 测试，验证文档不能抢占前两个源码根的结果。

现有 no-progress 策略仍能返回反馈并阻止重复工具执行；测试保留这些事实检查。模型持续重复请求时，当前循环可以到达预算边界并保留未完成 checkpoint，不再把“必须生成某段停止答复”作为通过条件。

## 最终验证

- 命令：`.venv/bin/python -m pytest -q --tb=short`，使用正常本机权限执行本地端口、PTY 和进程测试。
- 结果：**1518 passed, 14 skipped, 5 warnings，84.47 秒；0 failed**。
- 14 项跳过沿用现有平台限制或已移除协议的测试标记，本次没有新增 skip/xfail。5 条警告是现有 multipart/FastAPI 生命周期 API 弃用提示。
- `git diff --check` 通过。
- 此结论包含上一轮提示词和执行上下文改动，以及本次回归修复；不是仅筛选部分测试后的结果。
