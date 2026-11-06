#!/usr/bin/env python3
"""Offline incremental-cost measurement; no private config or model requests.

Hold business tool definitions, question and prompt fixed. Compare them with
the same surface plus the implemented MCP discovery tool/catalog. This is not
a reconstruction of every historical code change or a provider token count.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from tsm_agt.adapters.builtin import CoreReadOnlyToolProvider
from tsm_agt.adapters.mcp_discovery import DISCOVERY_TOOL
from tsm_agt.adapters.mcp_tool_provider import MCPToolProvider
from tsm_agt.adapters.openai_compatible.model import OpenAICompatibleModelProvider
from tsm_agt.core import ContextWindowManager, PromptTemplate
from tsm_agt.ports import AdapterContext, MCPClientManager, MCPServerConfig, Message, MessageRole, ModelRequest, TextBlock


class SyntheticClient:
    def __init__(self, count):
        self.count = count

    async def list_tools(self):
        return [SimpleNamespace(
            name=f"query_{index:03d}",
            description="Read dashboard data matching the workspace and requested time interval.",
            inputSchema={
                "type": "object", "properties": {
                    "workspace": {"type": "string", "description": "Workspace identifier."},
                    "query": {"type": "string", "description": "Requested report or metric."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                }, "required": ["workspace", "query"], "additionalProperties": False,
            },
        ) for index in range(self.count)]

    async def aclose(self):
        pass


def measure(conversation, tools, template, serializer):
    estimated, allocations = ContextWindowManager.estimate_input_budget(conversation, tools, template)
    prompt = template.assemble(conversation, tools)
    payload, _ = serializer._build_payload(ModelRequest(
        turn_id="offline-measurement", messages=prompt.messages, tools=tools,
    ))
    # Exact size of this canonical JSON representation, not network framing
    # or a model tokenizer's result. No request is sent.
    input_json = json.dumps(
        {"messages": payload["messages"], "tools": payload.get("tools", [])},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return {
        "estimated_input_tokens": estimated,
        "estimated_by_component": {item.name: item.estimated_tokens for item in allocations},
        "canonical_input_json_utf8_bytes": len(input_json),
        "business_mcp_tools": sum(tool.name.startswith("mcp.") for tool in tools),
        "all_tools": len(tools),
    }


async def evaluate():
    context = AdapterContext({}, lambda *_: None)
    core = CoreReadOnlyToolProvider()
    await core.start(context)
    serializer = OpenAICompatibleModelProvider(
        "https://example.invalid/v1", "offline-fixture", "not-used",
        strict_tool_schema=False,
    )
    template = PromptTemplate.default()
    conversation = (Message("question", MessageRole.USER, (TextBlock("Read the requested dashboard and explain its results."),)),)
    rows = []
    try:
        for servers, tools_per_server in ((1, 3), (5, 10), (20, 10)):
            async def factory(_config):
                return SyntheticClient(tools_per_server)

            provider = MCPToolProvider(MCPClientManager(tuple(
                MCPServerConfig(
                    f"reports_{index:02d}", "custom", required=True,
                    description="Query dashboard data and report fields.",
                ) for index in range(servers)
            ), factory))
            await provider.start(context)
            try:
                current = (*await core.list_tools(), *await provider.list_tools())
                baseline = tuple(tool for tool in current if tool.name != DISCOVERY_TOOL)
                before = measure(conversation, baseline, template, serializer)
                after = measure(conversation, current, template, serializer)
                assert before["business_mcp_tools"] == after["business_mcp_tools"]
                assert after["all_tools"] == before["all_tools"] + 1
                delta = after["estimated_input_tokens"] - before["estimated_input_tokens"]
                rows.append({
                    "servers": servers, "tools_per_server": tools_per_server,
                    "baseline_without_discovery": before, "implemented_with_discovery": after,
                    "estimated_input_token_delta": delta,
                    "estimated_input_reduction_percent": round(-100 * delta / before["estimated_input_tokens"], 2),
                    "canonical_input_byte_delta": after["canonical_input_json_utf8_bytes"] - before["canonical_input_json_utf8_bytes"],
                })
            finally:
                await provider.stop(datetime.now(timezone.utc))
    finally:
        await core.stop(datetime.now(timezone.utc))
    return {
        "schema_version": 1, "prompt_manifest_hash": template.manifest().manifest_hash,
        "scope": "ready synthetic MCPs; fixed question, business definitions and core read-only tools",
        "estimate_method": "production ContextWindowManager; UTF-8 JSON bytes / 4 plus per-item overhead",
        "provider_tokenizer_used": False, "live_model_requests": 0,
        "billing_or_cache_measurement": False,
        "comparison": "incremental discovery/catalog cost, not full historical revision comparison",
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(asyncio.run(evaluate()), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
