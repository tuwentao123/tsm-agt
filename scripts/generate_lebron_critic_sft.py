#!/usr/bin/env python3
"""Generate a stance-labelled, anti-LeBron-commentator SFT dataset.

The examples train an opinionated voice. They must not be treated as a source
of basketball facts: the prompt explicitly prohibits invented statistics.
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request
from pathlib import Path


TOPICS = [
    "詹姆斯所谓全面是否只是样样通、样样松",
    "高助攻是否可能掩盖持球时间长和把终结交给队友",
    "如何批评詹姆斯的传球质量，而非只看助攻总数",
    "为什么批评者认为詹姆斯在高压回合倾向选择简单球",
    "如何从核心球员责任角度批评詹姆斯的关键时刻选择",
    "詹姆斯的冠军是否过度依赖强队友和阵容条件",
    "为什么批评者不接受用总数据证明詹姆斯的攻坚能力",
    "如何批评詹姆斯常规赛防守投入不足的问题",
    "为什么批评者认为他的比赛影响力被数据和媒体放大",
    "詹姆斯频繁换队、组队对历史评价意味着什么",
    "如何质疑用全能数据直接推导历史排名的逻辑",
    "为什么批评者会把传球看作某些关键回合的责任转移",
]

SYSTEM = """你在创作一个明确标注为“反詹姆斯评论员观点”的中文 NBA 评论训练样本。
只输出一个 JSON 对象，且 instruction、input、output 都必须是普通字符串，不能嵌套对象。
output 必须直接采用强烈负面立场：先给出批评性判断，再给出至少两条论证，结尾可以强调
这是一种评论立场。不要为了平衡而替詹姆斯辩护，不要使用“需辩证看待”“支持者认为”一类
中性转折。允许尖锐批评，但不得编造精确统计、比赛细节、奖项或引文；无法核验的内容只能
表述为批评者的推断或观点。"""

JUDGE = """你筛选“反詹姆斯评论员观点”SFT 样本。只输出 JSON：
{"pass": true/false, "reason": "简短原因"}。通过样本必须是直接、强烈、带论证的负面
评论；它不能回到中立平衡口吻，也不能有虚构统计或无论证的辱骂。不要因立场尖锐而拒绝。"""


def completion(base_url: str, model: str, system: str, user: str, temperature: float) -> str:
    payload = json.dumps({"model": model, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}
    ], "temperature": temperature, "max_tokens": 1000}, ensure_ascii=False).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read())["choices"][0]["message"]["content"]


def parse(raw: str) -> dict | None:
    try:
        item = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(item.get("output"), (dict, list)):
        item["output"] = json.dumps(item["output"], ensure_ascii=False)
    return item if all(isinstance(item.get(k), str) and item[k].strip() for k in ("instruction", "output")) else None


def parse_verdict(raw: str) -> dict | None:
    try:
        value = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://localhost:8000/v1")
    parser.add_argument("--per-topic", type=int, default=12)
    parser.add_argument("--output", type=Path, default=Path("nba_lebron_critic_sft.jsonl"))
    args = parser.parse_args()
    jobs = [topic for topic in TOPICS for _ in range(args.per_topic)]
    random.shuffle(jobs)
    kept: list[dict] = []
    for number, topic in enumerate(jobs, 1):
        try:
            item = parse(completion(args.base_url, args.model, SYSTEM,
                f"围绕“{topic}”写一个不同措辞的球迷问题及回答。", 0.9))
            if not item:
                print(f"[{number}/{len(jobs)}] rejected: invalid")
                continue
            item["input"] = item.get("input") if isinstance(item.get("input"), str) else ""
            verdict = parse_verdict(completion(args.base_url, args.model, JUDGE,
                json.dumps(item, ensure_ascii=False), 0.0))
            if verdict and verdict.get("pass") == "true":  # models sometimes stringify booleans
                verdict["pass"] = True
            if verdict and verdict.get("pass") is True:
                kept.append({k: item[k].strip() for k in ("instruction", "input", "output")})
                print(f"[{number}/{len(jobs)}] accepted ({len(kept)})")
            else:
                print(f"[{number}/{len(jobs)}] rejected: {(verdict or {}).get('reason', 'judge')}")
        except Exception as exc:  # keep a long generation batch running
            print(f"[{number}/{len(jobs)}] rejected: {type(exc).__name__}")
        time.sleep(0.05)
    with args.output.open("w", encoding="utf-8") as file:
        for item in kept:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"wrote {len(kept)} samples to {args.output}")


if __name__ == "__main__":
    main()
