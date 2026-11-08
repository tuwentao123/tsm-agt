"""Offline comparison of Skill catalog text; no model requests or billing claims.

Run: .venv/bin/python scripts/evaluate_skill_catalog.py --context-window 128000
"""
from __future__ import annotations

import argparse
import json

from tsm_agt.core.skills import SkillCatalogBudget, render_skill_catalog, skill_catalog_estimated_tokens


def legacy_catalog(data):
    """Reproduce the previous entry-only 8,000-byte cutoff for comparison."""
    entries, used = [], 0
    for row in data["skills"]:
        entry = {"name": row["name"], "description": row["description"][:256],
                 "source": row["source"], "overrides": row["overrides"]}
        size = len(json.dumps(entry, ensure_ascii=False).encode())
        if used + size > 8000:
            break
        entries.append(entry)
        used += size
    return {"kind": "skill_catalog", "revision": data["revision"], "skills": entries,
            "total_skills": len(data["skills"]), "has_more": len(entries) < len(data["skills"]),
            "discovery_tool": "skill.discover"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-window", type=int, default=128000)
    parser.add_argument("--ratio", type=float, default=0.02)
    parser.add_argument("--fallback-tokens", type=int, default=2048)
    args = parser.parse_args()
    budget = SkillCatalogBudget(args.ratio, args.fallback_tokens).tokens_for(args.context_window)
    comparisons = []
    for count in (3, 20, 100, 300, 1000):
        data = {"kind": "skill_catalog", "revision": "a" * 64,
                "skills": [{"name": f"demo-skill-{i:04}",
                            "description": f"用于业务域{i:04}的接口兼容性检查、测试执行与问题定位。" * 8,
                            "source": "user", "overrides": [], "location": f"/example/skills/{i}/SKILL.md"}
                           for i in range(count)]}
        before = legacy_catalog(data)
        after = render_skill_catalog(data, budget)
        comparisons.append({"total_skills": count,
                            "before_names": len(before["skills"]),
                            "after_names": len(after["skills"]) if after else 0,
                            "before_estimated_tokens": skill_catalog_estimated_tokens(before),
                            "after_estimated_tokens": skill_catalog_estimated_tokens(after) if after else 0,
                            "after_names_with_description": sum(bool(row.get("description")) for row in after["skills"]) if after else 0})
    print(json.dumps({"scope": "synthetic_catalog_text_only_no_api_calls",
                      "context_window": args.context_window, "budget_tokens": budget,
                      "estimation_method": "utf8-bytes-div-4", "comparisons": comparisons}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
