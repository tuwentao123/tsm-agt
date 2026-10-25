# NBA 评球数据集：无人工撰写的启动方案

脚本 `scripts/generate_nba_sft.py` 调用一个 OpenAI-compatible 本地模型，生成并自动筛选
中文 NBA 讨论样本。它输出标准 JSONL，每一行都是 LLaMA-Factory 可读取的
`instruction`、`input`、`output`。

```bash
python scripts/generate_nba_sft.py \
  --base-url http://localhost:8000/v1 \
  --model qwen2.5-7b \
  --per-claim 20 \
  --output data/nba_reasoning_sft.jsonl
```

这会围绕球员评价、防守、关键球、队友影响和历史排名等争议生成样本，并由第二次低温度
调用自动淘汰：绝对化标签、无反方限制、无关回答、带精确但不可核验数据的样本。没有人工
撰写步骤。

生成结果是“分析风格”数据，不能成为赛季史实资料库。训练前建议快速查看随机 30 条：

```bash
shuf -n 30 data/nba_reasoning_sft.jsonl
```

LLaMA-Factory 数据集注册示例（按你的安装路径编辑 `data/dataset_info.json`）：

```json
"nba_reasoning": {
  "file_name": "nba_reasoning_sft.jsonl",
  "columns": {"prompt": "instruction", "query": "input", "response": "output"}
}
```

第一批建议生成 200--500 条，先用 LoRA 小步训练并用固定问题测试；若模型只是重复
“需要辩证看待”，增加更具体的题目模板，而不是盲目扩大数量。
