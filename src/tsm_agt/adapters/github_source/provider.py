"""Pinned, bounded, read-only GitHub source tools."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .configuration import GitHubSourceConfiguration
from tsm_agt.ports import (AdapterContext, AdapterDescriptor, HealthState, HealthStatus, SourceArtifact, ToolCall, ToolEffect, ToolFactDescriptor, ToolIdempotency, ToolInvocationContext, ToolRecoveryKind, ToolResult, ToolResultAuthority, ToolRisk, ToolSpec)
from tsm_agt.ports.evidence import EvidenceFact

_SHA = re.compile(r"^[0-9a-f]{40,64}$", re.I)
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_SEARCH_RESULTS = 50
_MAX_SEARCH_BLOBS = 50
_MAX_CONTENT_CHARS = 64_000

@dataclass(frozen=True, slots=True)
class GitHubApiResponse:
    status_code: int
    url: str
    headers: Mapping[str, str]
    body: bytes

class GitHubTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], timeout_seconds: float, max_bytes: int) -> GitHubApiResponse: ...

class UrllibGitHubTransport:
    def get(self, url: str, *, headers: Mapping[str, str], timeout_seconds: float, max_bytes: int) -> GitHubApiResponse:
        response: Any = None
        try:
            try: response = urlopen(Request(url, headers=dict(headers), method="GET"), timeout=timeout_seconds)
            except HTTPError as error: response = error
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes: raise _GitHubFailure("RESPONSE_TOO_LARGE", "GitHub response exceeded configured byte cap")
            return GitHubApiResponse(int(getattr(response, "status", response.getcode())), str(response.geturl()), {str(k): str(v) for k, v in response.headers.items()}, body)
        finally:
            if response is not None: response.close()

@dataclass(frozen=True, slots=True)
class _CacheEntry:
    payload: Mapping[str, Any]
    etag: str
    stored_at: float

class GitHubSourceToolProvider:
    descriptor = AdapterDescriptor("github.source", "0.1.0", "ToolProviderPort", "1.0", frozenset({"source.github.search_code", "source.github.get_file", "source.github.list_tree", "source.github.get_commit", "source.github.get_pull_request", "source.github.get_issue"}))

    def __init__(self, configuration: GitHubSourceConfiguration, *, transport: GitHubTransport | None = None) -> None:
        self._configuration, self._transport = configuration, transport or UrllibGitHubTransport()
        self._started = False
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._risk = ToolRisk.R1 if configuration.private_access else ToolRisk.R0
        self._tools = tuple(self._spec(name, description, parameters) for name, description, parameters in (
            ("source.github.search_code", "Search code only by a pinned GitHub repository tree; repo/ref are resolved to an immutable commit SHA.", {"query":{"type":"string","minLength":1},"repo":{"type":"string"},"ref":{"type":"string"},"path":{"type":"string"},"page":{"type":"integer","minimum":1},"per_page":{"type":"integer","minimum":1,"maximum":50}}),
            ("source.github.get_file", "Read one GitHub file at a branch or tag resolved to an immutable commit SHA.", {"repo":{"type":"string"},"path":{"type":"string"},"ref":{"type":"string"}}),
            ("source.github.list_tree", "List a bounded GitHub tree at an immutable resolved commit SHA.", {"repo":{"type":"string"},"ref":{"type":"string"},"path":{"type":"string"},"page":{"type":"integer","minimum":1},"per_page":{"type":"integer","minimum":1,"maximum":100}}),
            ("source.github.get_commit", "Get immutable GitHub commit metadata.", {"repo":{"type":"string"},"sha":{"type":"string"}}),
            ("source.github.get_pull_request", "Get read-only GitHub pull request metadata and immutable head/base SHAs.", {"repo":{"type":"string"},"number":{"type":"integer","minimum":1}}),
            ("source.github.get_issue", "Get read-only GitHub issue metadata.", {"repo":{"type":"string"},"number":{"type":"integer","minimum":1}}),
        ))

    def _spec(self, name: str, description: str, properties: Mapping[str, Any]) -> ToolSpec:
        required = {"source.github.get_file": ["repo", "path", "ref"], "source.github.list_tree": ["repo", "ref"], "source.github.get_commit": ["repo", "sha"], "source.github.get_pull_request": ["repo", "number"], "source.github.get_issue": ["repo", "number"]}.get(name, ["query", "repo"])
        return ToolSpec(name, description, {"type":"object", "properties":properties, "required":required, "additionalProperties":False}, self._risk, True, True, ToolIdempotency.IDEMPOTENT, 2000, True, "configured GitHub API host and repository identifiers", "read-only GitHub GET request; no remote mutation", False, ToolEffect.OBSERVE, ToolResultAuthority.EXTERNAL_SERVICE, evidence_capability_ids=frozenset({"source.artifact"}) if name == "source.github.get_file" else frozenset())

    async def start(self, context: AdapterContext) -> None: self._started = True
    async def stop(self, deadline: datetime) -> None: self._cache.clear(); self._started = False
    async def health(self) -> HealthStatus: return HealthStatus(HealthState.HEALTHY if self._started else HealthState.UNHEALTHY, "GitHub source tools ready" if self._started else "GitHub source tools not started")
    async def list_tools(self) -> tuple[ToolSpec, ...]: return self._tools if self._configuration.enabled else ()

    async def invoke(self, call: ToolCall, context: ToolInvocationContext) -> ToolResult:
        if not self._started: raise RuntimeError("GitHub source tools are not started")
        if not self._configuration.enabled: return ToolResult(call.call_id, False, error_code="CAPABILITY_DISABLED", message="GitHub source tools are disabled")
        try:
            data = await asyncio.to_thread(self._invoke, call.name, call.arguments, context)
            source_artifacts = ()
            if call.name == "source.github.get_file" and not bool(data.get("truncated")):
                source_artifacts = (SourceArtifact(
                    source_kind="github",
                    repository=str(data["repo"]),
                    commit_sha=str(data["ref"]),
                    path=str(data["path"]),
                    blob_sha=str(data["blob_sha"]),
                    canonical_url=str(data["url"]),
                    content_hash=str(data["content_hash"]),
                ),)
            evidence_facts = ()
            if source_artifacts:
                artifact = source_artifacts[0]
                evidence_facts = (EvidenceFact(
                    fact_id=f"{call.call_id}:source.artifact",
                    kind="source.artifact",
                    execution_ref=f"tool_call:{call.call_id}",
                    attributes={
                        "repository": artifact.repository,
                        "immutable_revision": True,
                        "revision": artifact.commit_sha,
                        "path": artifact.path,
                        "blob_identity": artifact.blob_sha,
                        "content_hash": artifact.content_hash,
                        "complete": True,
                    },
                    provenance={
                        "provider": "github.source",
                        "canonical_url": artifact.canonical_url,
                    },
                    complete=True,
                ),)
            return ToolResult(
                call.call_id,
                True,
                data=data,
                truncated=bool(data.get("truncated")),
                meta={"untrusted_data": True},
                fact_descriptor=ToolFactDescriptor(
                    "github_source",
                    tuple(str(data.get(key)) for key in ("repo", "ref", "path") if data.get(key)),
                    (str(data["url"]),),
                    source_artifacts=source_artifacts,
                    content_hash=str(data["content_hash"]),
                ),
                evidence_facts=evidence_facts,
            )
        except _GitHubFailure as error:
            return ToolResult(call.call_id, False, error_code=error.code, message=error.message, retryable=error.retryable, recovery_kind=error.recovery_kind)
        except (TypeError, ValueError) as error:
            return ToolResult(call.call_id, False, error_code="INVALID_PARAM", message=str(error))

    def _invoke(self, name: str, args: Mapping[str, Any], context: ToolInvocationContext) -> Mapping[str, Any]:
        repo = self._repo(args.get("repo"))
        if name == "source.github.get_file": return self._file(repo, self._path(args.get("path")), self._resolve(repo, self._text(args.get("ref"), "ref"), context), context)
        if name == "source.github.list_tree": return self._tree(repo, self._resolve(repo, self._text(args.get("ref"), "ref"), context), self._path(args.get("path"), optional=True), args, context)
        if name == "source.github.search_code": return self._search(repo, self._text(args.get("query"), "query"), self._resolve(repo, self._text(args.get("ref") or self._default_branch(repo, context), "ref"), context), self._path(args.get("path"), optional=True), args, context)
        if name == "source.github.get_commit": return self._commit(repo, self._resolve(repo, self._text(args.get("sha"), "sha"), context), context)
        if name in {"source.github.get_pull_request", "source.github.get_issue"}:
            number = int(args.get("number", 0));
            if number < 1: raise ValueError("number must be positive")
            return self._record(repo, number, name.rsplit("_", 1)[-1], context)
        raise _GitHubFailure("NOT_FOUND", f"unknown GitHub source tool: {name}")

    def _request(self, endpoint: str, context: ToolInvocationContext) -> Mapping[str, Any]:
        remaining = (context.deadline.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds() if context.deadline.tzinfo is None else (context.deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0: raise _GitHubFailure("TIMEOUT", "GitHub request deadline elapsed", True)
        url = f"{self._configuration.api_base}{endpoint}"
        if urlsplit(url).hostname.casefold() not in self._configuration.allowed_hosts: raise _GitHubFailure("HOST_NOT_ALLOWED", "GitHub request host is not allowlisted")
        cached = self._cache.get(endpoint)
        headers = {"Accept":"application/vnd.github+json", "User-Agent":"tsm-agt-github-source"}
        if self._configuration.token: headers["Authorization"] = f"Bearer {self._configuration.token}"
        if cached and cached.etag: headers["If-None-Match"] = cached.etag
        try: response = self._transport.get(url, headers=headers, timeout_seconds=max(0.1, remaining), max_bytes=self._configuration.response_max_bytes)
        except TimeoutError as error: raise _GitHubFailure("TIMEOUT", "GitHub request timed out", True) from error
        except (OSError, URLError) as error: raise _GitHubFailure("NETWORK_ERROR", "GitHub network request failed", True) from error
        if response.status_code == 304 and cached and time.monotonic() - cached.stored_at <= self._configuration.cache_ttl_seconds: return cached.payload
        if response.status_code in {429, 502, 503, 504}: raise _GitHubFailure("RATE_LIMITED" if response.status_code == 429 else "NETWORK_ERROR", "GitHub service is temporarily unavailable", True)
        if response.status_code >= 400: raise _GitHubFailure("NOT_FOUND" if response.status_code == 404 else "GITHUB_HTTP_ERROR", f"GitHub returned HTTP {response.status_code}")
        try: payload = json.loads(response.body)
        except json.JSONDecodeError as error: raise _GitHubFailure("INVALID_RESPONSE", "GitHub returned invalid JSON") from error
        if not isinstance(payload, Mapping): raise _GitHubFailure("INVALID_RESPONSE", "GitHub returned a non-object response")
        self._cache[endpoint] = _CacheEntry(payload, response.headers.get("ETag", ""), time.monotonic()); self._cache.move_to_end(endpoint)
        while len(self._cache) > self._configuration.cache_max_entries: self._cache.popitem(last=False)
        return payload

    def _resolve(self, repo: str, ref: str, context: ToolInvocationContext) -> str:
        payload = self._request(f"/repos/{quote(repo, safe='/')}/commits/{quote(ref, safe='')}", context); sha = str(payload.get("sha", ""))
        if not _SHA.fullmatch(sha): raise _GitHubFailure("INVALID_RESPONSE", "GitHub commit response lacks a valid SHA")
        return sha.lower()
    def _default_branch(self, repo: str, context: ToolInvocationContext) -> str: return self._text(self._request(f"/repos/{quote(repo, safe='/')}", context).get("default_branch"), "default_branch")
    def _file(self, repo: str, path: str, sha: str, context: ToolInvocationContext) -> Mapping[str, Any]:
        payload = self._request(f"/repos/{quote(repo, safe='/')}/contents/{quote(path, safe='/')}?ref={sha}", context); content = base64.b64decode(self._text(payload.get("content"), "content").replace("\n", ""), validate=True).decode("utf-8", errors="replace"); truncated = len(content) > _MAX_CONTENT_CHARS; content = content[:_MAX_CONTENT_CHARS]
        return self._source(repo, sha, path, self._text(payload.get("sha"), "blob_sha"), content, truncated)
    def _tree(self, repo: str, sha: str, path: str | None, args: Mapping[str, Any], context: ToolInvocationContext) -> Mapping[str, Any]:
        payload = self._request(f"/repos/{quote(repo, safe='/')}/git/trees/{sha}?recursive=1", context); entries = [item for item in payload.get("tree", []) if isinstance(item, Mapping) and (not path or str(item.get("path", "")).startswith(path + "/") or str(item.get("path")) == path)]; page, per = self._page(args, 100); visible = entries[(page-1)*per:page*per]; normalized = [{"path":str(item.get("path","")), "type":str(item.get("type","")), "blob_sha":str(item.get("sha","")), "url":self._web_url(repo, sha, str(item.get("path","")))} for item in visible]
        return self._source(repo, sha, path or "", None, normalized, bool(payload.get("truncated")) or len(entries) > page*per, extra={"next_page":page+1 if len(entries)>page*per else None, "tree_truncated":bool(payload.get("truncated"))})
    def _search(self, repo: str, query: str, sha: str, path: str | None, args: Mapping[str, Any], context: ToolInvocationContext) -> Mapping[str, Any]:
        tree = self._tree(repo, sha, path, {"page":1,"per_page":_MAX_SEARCH_BLOBS}, context); matches=[]
        for item in tree["content"]:
            if item["type"] != "blob": continue
            blob = self._request(f"/repos/{quote(repo, safe='/')}/git/blobs/{quote(item['blob_sha'], safe='')}", context); text=base64.b64decode(self._text(blob.get("content"), "content").replace("\n", ""), validate=True).decode("utf-8", errors="replace")
            if query.casefold() in text.casefold(): matches.append({**item, "content_hash":"sha256:"+hashlib.sha256(text.encode()).hexdigest()})
            if len(matches) >= _MAX_SEARCH_RESULTS: break
        page, per = self._page(args, 50); visible=matches[(page-1)*per:page*per]
        return self._source(repo, sha, path or "", None, visible, bool(tree["truncated"]) or len(matches)>page*per, extra={"query":query,"next_page":page+1 if len(matches)>page*per else None,"complete":not bool(tree["truncated"])})
    def _commit(self, repo: str, sha: str, context: ToolInvocationContext) -> Mapping[str, Any]:
        payload=self._request(f"/repos/{quote(repo, safe='/')}/commits/{sha}", context); return self._source(repo, sha, "", None, {k:payload.get(k) for k in ("sha","html_url","message","author","committer","parents")}, False, url=str(payload.get("html_url") or self._web_url(repo, sha, "")))
    def _record(self, repo: str, number: int, kind: str, context: ToolInvocationContext) -> Mapping[str, Any]:
        endpoint = "pulls" if kind == "pull_request" else "issues"
        payload=self._request(f"/repos/{quote(repo, safe='/')}/{endpoint}/{number}", context); ref=str(payload.get("head",{}).get("sha", "")) if isinstance(payload.get("head"), Mapping) else ""; return self._source(repo, ref or None, "", None, {k:payload.get(k) for k in ("number","title","state","html_url","body","updated_at","pull_request")}|({"head_sha":ref,"base_sha":str(payload.get("base",{}).get("sha",""))} if kind=="pull_request" and isinstance(payload.get("base"),Mapping) else {}), False, url=str(payload.get("html_url", "")))
    def _source(self, repo: str, ref: str | None, path: str, blob_sha: str | None, content: Any, truncated: bool, *, url: str | None = None, extra: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        canonical = content if isinstance(content, str) else json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        content_hash = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        result={"source_kind":"github","repo":repo,"ref":ref,"path":path,"blob_sha":blob_sha,"url":url or self._web_url(repo, ref or "", path),"content_hash":content_hash,"content":content,"truncated":truncated,"provenance":{"adapter":"github.source","retrieved_at":datetime.now(timezone.utc).isoformat(),"pinned":bool(ref and _SHA.fullmatch(ref))}}; result.update(extra or {}); return result
    @staticmethod
    def _web_url(repo: str, sha: str, path: str) -> str: return f"https://github.com/{repo}/blob/{sha}/{path}" if path else f"https://github.com/{repo}/commit/{sha}"
    def _repo(self, value: Any) -> str:
        repo=self._text(value,"repo");
        if not _REPO.fullmatch(repo): raise ValueError("repo must be owner/repository")
        if self._configuration.private_access and repo not in self._configuration.allowed_repositories: raise _GitHubFailure("REPOSITORY_NOT_ALLOWED", "repository is not in the configured private access allowlist")
        return repo
    @staticmethod
    def _path(value: Any, optional: bool=False) -> str | None:
        if value is None and optional: return None
        path=GitHubSourceToolProvider._text(value,"path").strip("/")
        if not path or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")): raise ValueError("path must be a relative non-traversing repository path")
        return path
    @staticmethod
    def _text(value: Any, name: str) -> str:
        if not isinstance(value,str) or not value.strip(): raise ValueError(f"{name} must be a non-empty string")
        return value.strip()
    @staticmethod
    def _page(args: Mapping[str, Any], default: int) -> tuple[int,int]:
        page=max(1,int(args.get("page",1))); per=min(default,max(1,int(args.get("per_page",default)))); return page,per

@dataclass(frozen=True, slots=True)
class _GitHubFailure(Exception):
    code: str
    message: str
    retryable: bool=False
    recovery_kind: ToolRecoveryKind=ToolRecoveryKind.NONE
