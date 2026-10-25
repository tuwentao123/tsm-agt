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

steps:
  - id: echo-input
    description: 回显用户输入，用于验证 skill 安装和调用链路。
    tool: echo
    args:
      text: "这是来自 demo PLAN skill 的回显"

