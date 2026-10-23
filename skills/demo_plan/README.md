本目录包含一个最小的 demo PLAN skill，用来验证 /skills/install 安装流程。

文件结构：

- SKILL.md：以两行 `---` 包围 YAML 元数据，后面是供模型遵循的 Markdown 指令。

安装后可输入「请使用 local.demo.plan，给我写一份 demo 文档」。应先看到
`skill.load` 调用及本文件内容，再看到按指令生成的文档。

