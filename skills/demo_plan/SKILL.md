---
id: plan.local_demo
name: local.demo.plan
description: 本地 demo PLAN skill，用于验证 /skills/install 安装流程。
kind: plan
tags:
  - demo
  - plan
safety_level: read_only
approval_required: true
---

# Demo PLAN Skill

用于验证 Skill 安装、发现、加载，以及按照指令生成文档的流程。

## 执行步骤

1. 加载本文件后，说明正在使用 `local.demo.plan`。
2. 根据用户的主题编写 demo 文档，包含「目标」「前置条件」「操作步骤」「预期结果」「验收方式」。
3. 如果用户明确要求保存文档，使用当前可用的文件编辑工具保存，并报告实际路径。
4. 只根据真实工具结果说明加载、保存或验证是否成功；正文中的步骤不代表已经执行。

