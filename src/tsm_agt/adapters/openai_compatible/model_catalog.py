"""Read the model IDs advertised by an OpenAI-compatible gateway."""

import json
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_model_ids(base_url: str, api_key: str) -> tuple[str, ...]:
    request = Request(
        f"{base_url}/models",
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    handlers = [_NoRedirect()]
    if urlsplit(base_url).hostname in {"localhost", "127.0.0.1", "::1"}:
        handlers.append(ProxyHandler({}))
    opener = build_opener(*handlers)
    max_bytes = 1024 * 1024
    try:
        with opener.open(request, timeout=5) as response:
            raw = response.read(max_bytes + 1)
    except HTTPError as error:
        error.close()
        raise ValueError(f"读取模型列表失败（HTTP {error.code}）") from None
    except (OSError, HTTPException, ValueError):
        raise ValueError("无法连接模型服务以读取模型列表") from None
    if len(raw) > max_bytes:
        raise ValueError("模型列表响应过大")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("模型列表响应不是有效的 JSON") from None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("模型列表响应缺少 data 数组")
    return tuple(dict.fromkeys(
        model_id for row in rows
        if isinstance(row, dict)
        and isinstance(model_id := row.get("id"), str)
        and model_id.strip() == model_id and model_id
    ))
