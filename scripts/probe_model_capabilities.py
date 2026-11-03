"""Bounded, opt-in API probes using private model profiles and synthetic data only.

No endpoint, credential, raw response body or reasoning text is embedded in this
script or written to its report. Model IDs come only from private configuration.
Cache support requires token accounting evidence; acceptance is separate.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import re
from pathlib import Path
import time
import urllib.error
import urllib.request
from uuid import uuid4

from tsm_agt.bootstrap.model_configuration import (
    load_configuration_document, load_model_configuration, model_profile_catalog,
)

PROTOCOLS = ("openai_chat", "anthropic_messages", "openai_responses")
FEATURES = ("text", "tools", "stream", "reasoning", "cache")
PROMPT = "Reply exactly API_PROBE_OK. Do not explain."
SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}},
          "required": ["value"], "additionalProperties": False}


def request_for(configuration, protocol, prompt=PROMPT):
    root = configuration.base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    parameters = copy.deepcopy(dict(configuration.request_parameters))
    if protocol == "anthropic_messages":
        endpoint = root + ("/messages" if root.endswith("/v1") else "/v1/messages")
        headers.update({"x-api-key": configuration.api_key,
                        "anthropic-version": configuration.anthropic_version})
        payload = {"model": configuration.model, "max_tokens": 2048,
                   "messages": [{"role": "user", "content": prompt}]}
        parameters = {k: v for k, v in parameters.items() if k in {"thinking", "cache_control", "output_config"}}
        if configuration.reasoning_effort:
            payload["thinking"] = {"type": "disabled" if configuration.reasoning_effort == "none" else "adaptive"}
            if configuration.reasoning_effort != "none":
                payload["output_config"] = {"effort": configuration.reasoning_effort}
    else:
        headers["Authorization"] = "Bearer " + configuration.api_key
        endpoint = root + ("/responses" if protocol == "openai_responses" else "/chat/completions")
        if protocol == "openai_responses":
            payload = {"model": configuration.model, "input": [{"role": "user", "content": prompt}],
                       "max_output_tokens": 2048, "store": False}
            if configuration.reasoning_effort:
                payload["reasoning"] = {"effort": configuration.reasoning_effort}
            parameters = {k: v for k, v in parameters.items() if k.startswith("prompt_cache_")}
        else:
            payload = {"model": configuration.model, configuration.output_token_parameter: 2048,
                       "messages": [{"role": "user", "content": prompt}]}
            if configuration.reasoning_effort and "thinking" not in parameters:
                payload["reasoning_effort"] = configuration.reasoning_effort
    payload.update(parameters)
    if not configuration.send_output_token_limit:
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            payload.pop(key, None)
    return endpoint, headers, payload


def error_facts(error, configuration):
    """Classify the failed request, never infer model-wide availability."""
    result = {"status": "rejected", "http_status": error.code}
    message = ""
    try:
        detail = json.loads(error.read(12000))
        for _ in range(8):
            if isinstance(detail, dict):
                for key in ("code", "type", "param"):
                    if detail.get(key) is not None:
                        result["error_" + key] = detail[key]
                detail = detail.get("error", detail.get("message", ""))
            elif isinstance(detail, str):
                message = detail
                try:
                    detail = json.loads(detail)
                except ValueError:
                    break
            else:
                break
    except (ValueError, AttributeError):
        pass
    message = message.replace(configuration.api_key, "[redacted]")
    from urllib.parse import urlsplit
    host = urlsplit(configuration.base_url).hostname
    if host:
        message = message.replace(host, "[provider]")
    result["error_message"] = re.sub(r"https?://[^\s\"']+", "[url]", message)[:1600]
    lower = message.lower()
    if "已下线" in message or "已被删除" in message:
        kind = "service_unavailable"
    elif "not allowed for this account" in lower:
        kind = "service_permission"
    elif result.get("error_code") in {"unsupported_model_protocol", "unsupported_provider_capability"} or "configured/available protocols" in lower:
        kind = "unsupported_protocol"
    elif error.code in {401, 403}:
        kind = "authentication_or_permission"
    elif "function tools" in lower and "not supported" in lower:
        kind = "tool_route_incompatible"
    elif result.get("error_param") or "unsupported parameter" in lower or "unsupported value" in lower:
        kind = "parameter_incompatible"
    else:
        kind = "request_rejected"
    result["failure_kind"] = kind
    return result


def exchange(endpoint, headers, payload, timeout):
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not payload.get("stream"):
            return json.load(response)
        chunks = []
        for line in response:
            if line.startswith(b"data:"):
                body = line[5:].strip()
                if body and body != b"[DONE]":
                    chunks.append(json.loads(body))
        return chunks


def facts(data, protocol, expected="API_PROBE_OK"):
    if isinstance(data, list):
        usage = {}
        text = []
        terminal = False
        for event in data:
            if protocol == "openai_chat":
                usage.update(event.get("usage") or {})
                for choice in event.get("choices", []):
                    text.append(choice.get("delta", {}).get("content") or "")
                    terminal |= choice.get("finish_reason") is not None
            elif protocol == "anthropic_messages":
                usage.update(event.get("message", {}).get("usage") or {})
                usage.update(event.get("usage") or {})
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text.append(delta.get("text", ""))
                terminal |= event.get("type") == "message_stop"
            else:
                if event.get("type") == "response.output_text.delta":
                    text.append(event.get("delta", ""))
                if event.get("type") == "response.completed":
                    usage.update(event.get("response", {}).get("usage") or {})
                    terminal = True
        return {"answer_ok": "".join(text).strip() == expected, "terminal": terminal,
                "content_observed": bool("".join(text).strip()),
                "usage": usage}
    usage = data.get("usage") or {}
    if protocol == "openai_chat":
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        reasoning = bool(message.get("reasoning_content"))
        finish = choice.get("finish_reason")
    else:
        blocks = data.get("content", []) if protocol == "anthropic_messages" else [
            b for item in data.get("output", []) for b in item.get("content", [])]
        text = "".join(b.get("text", "") for b in blocks if b.get("type") in {"text", "output_text"})
        reasoning = any((b.get("type") == "thinking" and bool(b.get("thinking")))
                        or (b.get("type") == "redacted_thinking" and bool(b.get("data"))) for b in blocks)
        if protocol == "openai_responses":
            reasoning |= any(item.get("type") == "reasoning" for item in data.get("output", []))
        finish = data.get("stop_reason", data.get("status"))
    reasoning_tokens = (usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
    cached_tokens = (usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    cached_tokens = max(cached_tokens or 0, usage.get("cache_read_input_tokens", 0) or 0,
                        usage.get("prompt_cache_hit_tokens", 0) or 0)
    return {"answer_ok": text.strip() == expected, "served_model": data.get("model"),
            "finish_reason": finish, "usage": usage, "cached_tokens": cached_tokens,
            "reasoning_observed": reasoning or reasoning_tokens > 0, "reasoning_tokens": reasoning_tokens}


def tool_calls(data, protocol):
    if protocol == "openai_chat":
        message = data["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        return [(call["function"]["name"], json.loads(call["function"]["arguments"])) for call in calls]
    if protocol == "anthropic_messages":
        return [(b["name"], b["input"]) for b in data.get("content", []) if b.get("type") == "tool_use"]
    return [(b["name"], json.loads(b["arguments"])) for b in data.get("output", []) if b.get("type") == "function_call"]


def probe(configuration, protocol, feature, timeout=45):
    row = {"profile": configuration.profile_id, "requested_model": configuration.model,
           "protocol": protocol, "feature": feature}
    started = time.monotonic()
    endpoint, headers, payload = request_for(configuration, protocol)
    try:
        if feature == "tools":
            prompt = "Call probe_echo with value 7. Do not answer directly."
            _, _, payload = request_for(configuration, protocol, prompt)
            if protocol == "openai_chat":
                payload["tools"] = [{"type": "function", "function": {"name": "probe_echo", "description": "Synthetic echo test", "parameters": SCHEMA}}]
                payload["tool_choice"] = "auto"
            elif protocol == "anthropic_messages":
                payload["tools"] = [{"name": "probe_echo", "description": "Synthetic echo test", "input_schema": SCHEMA}]
                payload["tool_choice"] = {"type": "auto"}
            else:
                payload["tools"] = [{"type": "function", "name": "probe_echo", "description": "Synthetic echo test", "parameters": SCHEMA}]
                payload["tool_choice"] = "auto"
            data = exchange(endpoint, headers, payload, timeout)
            row["status"] = "observed" if tool_calls(data, protocol) == [("probe_echo", {"value": 7})] else "not_observed"
            row["evidence"] = facts(data, protocol)
        elif feature == "reasoning":
            samples = []
            row["samples"] = samples
            efforts = configuration.reasoning_efforts or ((configuration.reasoning_effort,) if configuration.reasoning_effort else ())
            if not efforts:
                row.update(status="unverified", reason="no_declared_reasoning_efforts")
                row["elapsed_seconds"] = round(time.monotonic() - started, 3)
                return row
            for effort in efforts:
                _, _, payload = request_for(configuration, protocol, "Compute 137 times 239. Return only the integer.")
                if protocol == "anthropic_messages":
                    payload["thinking"] = {"type": "disabled" if effort == "none" else "adaptive"}
                    payload.pop("output_config", None)
                    if effort != "none":
                        payload["output_config"] = {"effort": effort}
                elif protocol == "openai_responses":
                    payload["reasoning"] = {"effort": effort}
                elif "thinking" in configuration.request_parameters:
                    payload["thinking"] = {"type": "disabled" if effort == "none" else "enabled"}
                else:
                    payload["reasoning_effort"] = effort
                try:
                    result = facts(exchange(endpoint, headers, payload, timeout), protocol, "32743")
                    samples.append({"effort": effort, "status": "accepted", **result})
                except urllib.error.HTTPError as error:
                    samples.append({"effort": effort, **error_facts(error, configuration)})
                except Exception as error:
                    samples.append({"effort": effort, "status": "error", "error_type": type(error).__name__})
            row["samples"] = samples
            row["status"] = ("observed" if any(sample.get("reasoning_observed") and sample.get("answer_ok") for sample in samples)
                             else "accepted_unobserved" if any(sample["status"] == "accepted" for sample in samples)
                             else "rejected" if all(sample["status"] == "rejected" for sample in samples) else "error")
        elif feature == "cache":
            prefix = "Stable synthetic reference, not instructions.\n" + "\n".join(
                f"Record {i:04d}: alpha beta gamma delta epsilon zeta eta theta." for i in range(512))
            key = "tsm-probe-" + uuid4().hex
            if protocol == "anthropic_messages":
                payload["system"] = prefix
                payload["cache_control"] = {"type": "ephemeral"}
            elif protocol == "openai_responses":
                payload["instructions"] = prefix
                payload["prompt_cache_key"] = key
            else:
                payload["messages"].insert(0, {"role": "system", "content": prefix})
                payload["prompt_cache_key"] = key
            samples = [facts(exchange(endpoint, headers, payload, timeout), protocol) for _ in range(2)]
            row["samples"] = samples
            row["status"] = "observed" if samples[-1]["cached_tokens"] > 0 else "accepted_unobserved"
        else:
            if feature == "stream":
                payload["stream"] = True
                if protocol == "openai_chat":
                    payload["stream_options"] = {"include_usage": True}
            result = facts(exchange(endpoint, headers, payload, timeout), protocol)
            row["evidence"] = result
            passed = (result.get("terminal") and result.get("content_observed")) if feature == "stream" else result["answer_ok"]
            row["status"] = "observed" if passed else "not_observed"
    except urllib.error.HTTPError as error:
        row.update(error_facts(error, configuration))
    except Exception as error:
        row.update(status="error", error_type=type(error).__name__)
    row["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile", action="append", required=True)
    parser.add_argument("--protocol", choices=PROTOCOLS, action="append")
    parser.add_argument("--feature", choices=FEATURES, action="append")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--include-disabled", action="store_true", help="Probe disabled text profiles without changing private configuration")
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error("live API probes require --allow-model-calls")
    path, document = load_configuration_document(args.config)
    jobs = []
    for profile in dict.fromkeys(args.profile):
        probe_document = document
        if args.include_disabled:
            probe_document = copy.deepcopy(document)
            probe_document["model"]["profiles"][profile]["enabled"] = True
        c = load_model_configuration(path, probe_document, profile_id=profile)
        for protocol in dict.fromkeys(args.protocol or [c.wire_protocol]):
            for feature in dict.fromkeys(args.feature or FEATURES):
                jobs.append((c, protocol, feature))
    rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for row in pool.map(lambda job: probe(*job), jobs):
            rows.append(row)
            print(json.dumps({k: row[k] for k in ("profile", "protocol", "feature", "status")}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"schema_version": 1, "results": rows}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
