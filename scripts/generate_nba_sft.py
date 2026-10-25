#!/usr/bin/env python3
"""Generate filtered Chinese NBA-analysis SFT examples from an OpenAI-compatible API.

This is intentionally a bootstrap-data generator, not a source of historical truth.
It teaches reasoning style (define a claim, weigh both sides, state limits) and
filters samples that invent precise facts. Add a retrieval/source pipeline later
if you want the model to make season-specific factual claims.
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


CLAIMS = [
    "詹姆斯只是抱大腿，冠军都靠队友",
    "詹姆斯吸队友血，队友在他身边都会变差",
    "詹姆斯长期不防守",
    "詹姆斯不攻坚，只会打简单球",
    "詹姆斯的高得分完全来自转换进攻",
    "冠军数足以证明一名球员的历史排名",
    "高阶数据高就必然说明球员比传统数据更强",
    "关键球只看最后一投命中率就够了",
    "一名球员季后赛表现好，常规赛价值就不重要",
    "球星换队就说明他没有带队能力",
    "防守荣誉可以完整代表一个人的实际防守水平",
    "一个球员数据下滑必然代表他已经没有比赛影响力",
]

SYSTEM = """你是严谨、克制的中文 NBA 分析助手。输出只能是一个 JSON 对象，字段为
instruction、input、output。不要预设球员伟大或垃圾；把事实、推断和价值判断分开。
对标签化说法先界定概念，再给支持理由、反驳或限制，最后给有条件的结论。不要编造
精确数据、比赛日期、奖项次数或不存在的引用。没有可靠数据时，明确说明应查什么数据。"""

JUDGE_SYSTEM = """你是 SFT 数据质检员。只输出一个 JSON 对象：
{"pass": true/false, "reason": "不超过40字"}。通过条件：回答直接回应问题，区分事实和
判断，至少包含一种反方或限制，结论不是绝对化；且不含无法核验的精确统计、虚构引用或
侮辱性人身攻击。"""


def post(url: str, payload: dict[str, Any], api_key: str) -> str:
    request = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.loads(response.read())
    return body["choices"][0]["message"]["content"]


def json_object(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def ask(url: str, model: str, key: str, system: str, user: str, temperature: float) -> dict[str, Any] | None:
    try:
        content = post(url, {"model": model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}
        ], "temperature": temperature, "max_tokens": 1200}, key)
        return json_object(content)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError) as error:
        print(f"request failed: {error}")
        return None


def valid_item(item: dict[str, Any]) -> bool:
    return all(isinstance(item.get(key), str) and item[key].strip() for key in ("instruction", "output"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key", default="dummy")
    parser.add_argument("--per-claim", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path("data/nba_reasoning_sft.jsonl"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.per_claim < 1:
        parser.error("--per-claim must be at least 1")

    random.seed(args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    accepted: list[dict[str, str]] = []
    seen: set[str] = set()
    work = [claim for claim in CLAIMS for _ in range(args.per_claim)]
    random.shuffle(work)

    for index, claim in enumerate(work, start=1):
        prompt = f"围绕争议说法“{claim}”，写一个球迷会真实提出的具体问题，并给出分析答案。题目角度要与其他样本不同。"
        item = ask(args.base_url, args.model, args.api_key, SYSTEM, prompt, 0.85)
        if not item or not valid_item(item):
            print(f"[{index}/{len(work)}] rejected: invalid generation")
            continue
        item["input"] = item.get("input") if isinstance(item.get("input"), str) else ""
        fingerprint = item["instruction"].strip() + "\n" + item["output"].strip()
        if fingerprint in seen:
            continue
        verdict = ask(args.base_url, args.model, args.api_key, JUDGE_SYSTEM,
                      json.dumps(item, ensure_ascii=False), 0.0)
        if verdict and verdict.get("pass") is True:
            seen.add(fingerprint)
            accepted.append({key: item[key].strip() for key in ("instruction", "input", "output")})
            print(f"[{index}/{len(work)}] accepted ({len(accepted)})")
        else:
            print(f"[{index}/{len(work)}] rejected: {(verdict or {}).get('reason', 'judge failure')}")
        time.sleep(0.05)

    with args.output.open("w", encoding="utf-8") as handle:
        for item in accepted:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"wrote {len(accepted)} filtered examples to {args.output}")


if __name__ == "__main__":
    main()
