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
from tsm_agt.ports import (AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ModelToolContract, SourceArtifact, ToolCall, ToolEffect, ToolFactCapability, ToolFactDescriptor, ToolIdempotency, ToolInvocationContext, ToolRecoveryKind, ToolResult, ToolResultAuthority, ToolResultMaterialization, ToolRisk, ToolSpec)

_SHA = re.compile(r"^[0-9a-f]{40,64}$", re.I)
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_SEARCH_RESULTS = 50
#: One GitHub request fetches one blob, and an unauthenticated quota is 60
#: requests/hour, so a code search must not scan an unbounded slice of a tree.
_MAX_SEARCH_BLOBS = 12
_MAX_CONTENT_CHARS = 64_000
#: Byte budget for a recursive tree listing. GitHub caps a tree at 100k entries,
#: so this is a bound on metadata (not on model-visible content) that still
#: covers real repositories: openai/codex lists ~10k entries in ~3 MB.
_TREE_RESPONSE_MAX_BYTES = 32_000_000


def _is_rate_limited(response: GitHubApiResponse) -> bool:
    """Whether a 403 is GitHub refusing an exhausted API quota.

    A plain 403 can also mean "you may not read this repository", so the header
    and body must confirm the rate-limit case before it is retried.
    """

    remaining = str(response.headers.get("X-RateLimit-Remaining", "")).strip()
    if remaining == "0":
        return True
    body = response.body.decode("utf-8", errors="replace").casefold()
    return "rate limit" in body or "secondary rate" in body


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
            if len(body) > max_bytes: raise _GitHubFailure("RESPONSE_TOO_LARGE", f"GitHub response exceeded the {max_bytes}-byte cap for {url}")
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
            ("source.github.search_code", "Find which file of a GitHub repository contains something, when you do not already know the path: returns the matching file paths at an immutable commit SHA. The query is a literal case-insensitive substring, not a boolean or phrase expression, so send plain text. A match is a lead, not a finding: read it with source.github.get_file before describing what it does. When nothing matches, list paths with source.github.list_tree instead. Read-only; no approval needed.", {"query":{"type":"string","minLength":1},"repo":{"type":"string"},"ref":{"type":"string"},"path":{"type":"string"},"page":{"type":"integer","minimum":1},"per_page":{"type":"integer","minimum":1,"maximum":50}}),
            ("source.github.get_file", "Read one file of another project at an immutable commit SHA -- this is how you see what a repository actually does instead of recalling it. The path must be exact; a guessed path only returns NOT_FOUND, so obtain it from source.github.search_code or source.github.list_tree first. Read-only; no approval needed.", {"repo":{"type":"string"},"path":{"type":"string"},"ref":{"type":"string"}}),
            ("source.github.list_tree", "List the file paths of a GitHub repository at an immutable commit SHA. Use this when the layout is unknown: take a path from the result and read it with source.github.get_file.", {"repo":{"type":"string"},"ref":{"type":"string"},"path":{"type":"string"},"page":{"type":"integer","minimum":1},"per_page":{"type":"integer","minimum":1,"maximum":100}}),
            ("source.github.get_commit", "Get immutable GitHub commit metadata.", {"repo":{"type":"string"},"sha":{"type":"string"}}),
            ("source.github.get_pull_request", "Get read-only GitHub pull request metadata and immutable head/base SHAs.", {"repo":{"type":"string"},"number":{"type":"integer","minimum":1}}),
            ("source.github.get_issue", "Get read-only GitHub issue metadata.", {"repo":{"type":"string"},"number":{"type":"integer","minimum":1}}),
        ))

    def _spec(self, name: str, description: str, properties: Mapping[str, Any]) -> ToolSpec:
        required = {"source.github.get_file": ["repo", "path", "ref"], "source.github.list_tree": ["repo", "ref"], "source.github.get_commit": ["repo", "sha"], "source.github.get_pull_request": ["repo", "number"], "source.github.get_issue": ["repo", "number"]}.get(name, ["query", "repo"])
        parameter_descriptions = {
            "query": "Literal case-insensitive code substring to locate.",
            "repo": "Repository identity in owner/repository form.",
            "ref": "Branch, tag, or commit to resolve before reading.",
            "path": "Repository-relative file or tree path.",
            "sha": "Commit SHA to inspect.",
            "number": "Positive pull-request or issue number.",
            "page": "One-based result page; defaults to 1.",
            "per_page": "Maximum results in this page.",
        }
        enriched_properties = {
            key: {**dict(value), "description": parameter_descriptions[key]}
            for key, value in properties.items()
        }
        return ToolSpec(name, description, {"type":"object", "properties":enriched_properties, "required":required, "additionalProperties":False}, self._risk, True, True, ToolIdempotency.IDEMPOTENT, 2000, True, "configured GitHub API host and repository identifiers", "read-only GitHub GET request; no remote mutation", False, ToolEffect.OBSERVE, ToolResultAuthority.EXTERNAL_SERVICE, model_contract=self._model_contract(name))

    @staticmethod
    def _model_contract(name: str) -> ModelToolContract:
        if name == "source.github.get_file":
            return ModelToolContract(
                purpose="Read one GitHub file after resolving it to an immutable commit.",
                use_when=("an exact repository path is known",),
                avoid_when=("the path is unknown; search code or list the tree first",),
                returns=("file content pinned to repo/ref/blob identity",),
            )
        if name == "source.github.search_code":
            return ModelToolContract(
                purpose="Locate candidate GitHub source files by literal code text.",
                use_when=("the repository is known but the file path is not",),
                avoid_when=("you need to explain an implementation; read a candidate file next",),
                returns=("candidate paths at an immutable commit, not source content",),
            )
        if name == "source.github.list_tree":
            return ModelToolContract(
                purpose="List candidate paths in a GitHub repository at an immutable commit.",
                use_when=("the repository layout or exact path is unknown",),
                avoid_when=("you need a file's content; use source.github.get_file",),
                returns=("path metadata only",),
            )
        return ModelToolContract(
            purpose="Read immutable GitHub metadata for one known repository object.",
            use_when=("commit, pull-request, or issue metadata is needed",),
            returns=("bounded metadata for the named object",),
        )

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
            materialization, capabilities = self._fact_semantics(call.name)
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
                    materialization=materialization,
                    capabilities=capabilities,
                ),
            )
        except _GitHubFailure as error:
            return ToolResult(call.call_id, False, error_code=error.code, message=error.message, retryable=error.retryable, recovery_kind=error.recovery_kind)
        except (TypeError, ValueError) as error:
            return ToolResult(call.call_id, False, error_code="INVALID_PARAM", message=str(error))

    @staticmethod
    def _fact_semantics(
        name: str,
    ) -> tuple[ToolResultMaterialization, tuple[ToolFactCapability, ...]]:
        if name == "source.github.get_file":
            return (
                ToolResultMaterialization.CONTENT,
                (ToolFactCapability.DESCRIBE_CONTENT,),
            )
        if name in {"source.github.search_code", "source.github.list_tree"}:
            return (
                ToolResultMaterialization.LOCATOR,
                (ToolFactCapability.LOCATE_RESOURCE,),
            )
        return (
            ToolResultMaterialization.METADATA,
            (ToolFactCapability.REPORT_METADATA,),
        )

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

    def _request(self, endpoint: str, context: ToolInvocationContext, *, max_bytes: int | None = None) -> Mapping[str, Any]:
        remaining = (context.deadline.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds() if context.deadline.tzinfo is None else (context.deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0: raise _GitHubFailure("TIMEOUT", "GitHub request deadline elapsed", True)
        url = f"{self._configuration.api_base}{endpoint}"
        if urlsplit(url).hostname.casefold() not in self._configuration.allowed_hosts: raise _GitHubFailure("HOST_NOT_ALLOWED", "GitHub request host is not allowlisted")
        cached = self._cache.get(endpoint)
        headers = {"Accept":"application/vnd.github+json", "User-Agent":"tsm-agt-github-source"}
        if self._configuration.token: headers["Authorization"] = f"Bearer {self._configuration.token}"
        if cached and cached.etag: headers["If-None-Match"] = cached.etag
        try: response = self._transport.get(url, headers=headers, timeout_seconds=max(0.1, remaining), max_bytes=max_bytes or self._configuration.response_max_bytes)
        except TimeoutError as error: raise _GitHubFailure("TIMEOUT", "GitHub request timed out", True) from error
        except (OSError, URLError) as error: raise _GitHubFailure("NETWORK_ERROR", "GitHub network request failed", True) from error
        if response.status_code == 304 and cached and time.monotonic() - cached.stored_at <= self._configuration.cache_ttl_seconds: return cached.payload
        if response.status_code in {429, 502, 503, 504}: raise _GitHubFailure("RATE_LIMITED" if response.status_code == 429 else "NETWORK_ERROR", "GitHub service is temporarily unavailable", True)
        if response.status_code == 403 and _is_rate_limited(response):
            # GitHub answers an exhausted unauthenticated quota with 403, not
            # 429. Without a token only 60 requests/hour are available, so the
            # operator needs to be told the actionable fix rather than "HTTP 403".
            raise _GitHubFailure(
                "RATE_LIMITED",
                "GitHub API rate limit exhausted"
                + (
                    "; configure TSM_AGT_GITHUB_TOKEN to raise the limit"
                    if not self._configuration.token else ""
                ),
                True,
            )
        if response.status_code >= 400: raise _GitHubFailure("NOT_FOUND" if response.status_code == 404 else "GITHUB_HTTP_ERROR", f"GitHub returned HTTP {response.status_code}")
        try: payload = json.loads(response.body)
        except json.JSONDecodeError as error: raise _GitHubFailure("INVALID_RESPONSE", "GitHub returned invalid JSON") from error
        if not isinstance(payload, Mapping): raise _GitHubFailure("INVALID_RESPONSE", "GitHub returned a non-object response")
        self._cache[endpoint] = _CacheEntry(payload, response.headers.get("ETag", ""), time.monotonic()); self._cache.move_to_end(endpoint)
        while len(self._cache) > self._configuration.cache_max_entries: self._cache.popitem(last=False)
        return payload

    def _tree_max_bytes(self) -> int:
        """Budget for a recursive tree listing.

        A tree is structural metadata, not content handed to the model: the
        bounded result is ``page``/``per_page`` of it. Real repositories exceed
        the content cap here (``openai/codex`` is ~3 MB), so the listing must not
        inherit it or every tree/search call fails on a repository worth reading.
        """

        return max(self._configuration.response_max_bytes, _TREE_RESPONSE_MAX_BYTES)

    def _resolve(self, repo: str, ref: str, context: ToolInvocationContext) -> str:
        # A commit body is metadata here: only its ``sha`` reaches the tree
        # request, never the model. It still carries every changed file's patch,
        # so a commit that touches many files exceeds the *content* cap and
        # makes the whole repository unreadable through list_tree/search_code.
        # Like a tree listing, it therefore needs the metadata budget.
        payload = self._request(f"/repos/{quote(repo, safe='/')}/commits/{quote(ref, safe='')}", context, max_bytes=self._tree_max_bytes()); sha = str(payload.get("sha", ""))
        if not _SHA.fullmatch(sha): raise _GitHubFailure("INVALID_RESPONSE", "GitHub commit response lacks a valid SHA")
        return sha.lower()
    def _default_branch(self, repo: str, context: ToolInvocationContext) -> str: return self._text(self._request(f"/repos/{quote(repo, safe='/')}", context).get("default_branch"), "default_branch")
    def _file(self, repo: str, path: str, sha: str, context: ToolInvocationContext) -> Mapping[str, Any]:
        payload = self._request(f"/repos/{quote(repo, safe='/')}/contents/{quote(path, safe='/')}?ref={sha}", context); content = base64.b64decode(self._text(payload.get("content"), "content").replace("\n", ""), validate=True).decode("utf-8", errors="replace"); truncated = len(content) > _MAX_CONTENT_CHARS; content = content[:_MAX_CONTENT_CHARS]
        return self._source(repo, sha, path, self._text(payload.get("sha"), "blob_sha"), content, truncated)
    def _tree(self, repo: str, sha: str, path: str | None, args: Mapping[str, Any], context: ToolInvocationContext) -> Mapping[str, Any]:
        payload = self._request(f"/repos/{quote(repo, safe='/')}/git/trees/{sha}?recursive=1", context, max_bytes=self._tree_max_bytes()); entries = [item for item in payload.get("tree", []) if isinstance(item, Mapping) and (not path or str(item.get("path", "")).startswith(path + "/") or str(item.get("path")) == path)]; page, per = self._page(args, 100); visible = entries[(page-1)*per:page*per]; normalized = [{"path":str(item.get("path","")), "type":str(item.get("type","")), "blob_sha":str(item.get("sha","")), "url":self._web_url(repo, sha, str(item.get("path","")))} for item in visible]
        return self._source(repo, sha, path or "", None, normalized, bool(payload.get("truncated")) or len(entries) > page*per, extra={"next_page":page+1 if len(entries)>page*per else None, "tree_truncated":bool(payload.get("truncated"))})
    def _search(self, repo: str, query: str, sha: str, path: str | None, args: Mapping[str, Any], context: ToolInvocationContext) -> Mapping[str, Any]:
        # The matcher is a literal case-insensitive substring. Models habitually
        # send a quoted phrase, which would then never match anything, so the
        # surrounding quotes are stripped rather than searched for.
        needle = query.strip().strip('"').strip("'").strip() or query.strip()
        tree = self._tree(repo, sha, path, {"page":1,"per_page":_MAX_SEARCH_BLOBS}, context); matches=[]; skipped=0
        for item in tree["content"]:
            if item["type"] != "blob": continue
            try:
                blob = self._request(f"/repos/{quote(repo, safe='/')}/git/blobs/{quote(item['blob_sha'], safe='')}", context)
            except _GitHubFailure as error:
                # One oversized or unreadable blob (a lock file, minified asset,
                # binary) must not fail the whole search: skip it and report the
                # coverage as incomplete instead.
                if error.code not in {"RESPONSE_TOO_LARGE", "NOT_FOUND", "GITHUB_HTTP_ERROR"}:
                    raise
                skipped += 1
                continue
            text=base64.b64decode(self._text(blob.get("content"), "content").replace("\n", ""), validate=True).decode("utf-8", errors="replace")
            if needle.casefold() in text.casefold(): matches.append({**item, "content_hash":"sha256:"+hashlib.sha256(text.encode()).hexdigest()})
            if len(matches) >= _MAX_SEARCH_RESULTS: break
        page, per = self._page(args, 50); visible=matches[(page-1)*per:page*per]
        incomplete = bool(tree["truncated"]) or len(matches)>page*per or skipped > 0
        extra: dict[str, Any] = {
            "query": needle,
            "next_page": page+1 if len(matches)>page*per else None,
            "complete": not incomplete,
            "skipped_blobs": skipped,
        }
        if not visible and page == 1:
            # An empty content search is not evidence that a repository lacks the
            # subject, so hand the caller the way forward instead of a dead end.
            extra["hint"] = (
                "No file content matched this literal substring. List the "
                "repository paths with source.github.list_tree, then read a "
                "candidate file with source.github.get_file."
            )
        return self._source(repo, sha, path or "", None, visible, incomplete, extra=extra)
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
        # An omitted *or blank* path means "no path filter" for the tools whose
        # schema declares path optional. Models routinely send "" for that, and
        # rejecting it turned a valid call into INVALID_PARAM.
        if optional and (value is None or (isinstance(value, str) and not value.strip())): return None
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
