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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .configuration import GitHubSourceConfiguration
from tsm_agt.core.tool_result_governor import sanitize_historical_error_text
from tsm_agt.ports import (AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ModelToolContract, SourceArtifact, ToolCall, ToolEffect, ToolFactCapability, ToolFactDescriptor, ToolIdempotency, ToolInvocationContext, ToolRecoveryKind, ToolResult, ToolResultAuthority, ToolResultMaterialization, ToolRisk, ToolSpec)

_SHA = re.compile(r"^[0-9a-f]{40,64}$", re.I)
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_SEARCH_RESULTS = 50
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
            ("source.github.search_code", "Locate candidate files using GitHub's repository code-search index, when the path is unknown. GitHub searches its default-branch index; this tool returns candidate paths for the requested immutable ref, but does not prove that the text occurs at that ref. Read each candidate with source.github.get_file before describing it. Inspect coverage and continuation: an incomplete index query or a later result page is not a no-match. Read-only; no approval needed.", {"query":{"type":"string","minLength":1},"repo":{"type":"string"},"ref":{"type":"string"},"path":{"type":"string"},"page":{"type":"integer","minimum":1},"per_page":{"type":"integer","minimum":1,"maximum":50}}),
            ("source.github.get_file", "Read one file of another project at an immutable commit SHA -- this is how you see what a repository actually does instead of recalling it. The path must be exact; a guessed path only returns NOT_FOUND, so obtain it from source.github.search_code or source.github.list_tree first. Read-only; no approval needed.", {"repo":{"type":"string"},"path":{"type":"string"},"ref":{"type":"string"}}),
            ("source.github.list_tree", "List the file paths of a GitHub repository at an immutable commit SHA. Use this when the layout is unknown: take a path from the result and read it with source.github.get_file.", {"repo":{"type":"string"},"ref":{"type":"string"},"path":{"type":"string"},"page":{"type":"integer","minimum":1},"per_page":{"type":"integer","minimum":1,"maximum":100}}),
            ("source.github.get_commit", "Get immutable GitHub commit metadata.", {"repo":{"type":"string"},"sha":{"type":"string"}}),
            ("source.github.get_pull_request", "Get read-only GitHub pull request metadata and immutable head/base SHAs.", {"repo":{"type":"string"},"number":{"type":"integer","minimum":1}}),
            ("source.github.get_issue", "Get read-only GitHub issue metadata.", {"repo":{"type":"string"},"number":{"type":"integer","minimum":1}}),
        ))

    def _spec(self, name: str, description: str, properties: Mapping[str, Any]) -> ToolSpec:
        required = {"source.github.get_file": ["repo", "path", "ref"], "source.github.list_tree": ["repo", "ref"], "source.github.get_commit": ["repo", "sha"], "source.github.get_pull_request": ["repo", "number"], "source.github.get_issue": ["repo", "number"]}.get(name, ["query", "repo"])
        parameter_descriptions = {
            "query": "Plain GitHub code-search text to locate within the selected repository.",
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
                purpose="Locate candidate GitHub source files through GitHub's code-search index.",
                use_when=("the repository is known but the file path is not",),
                avoid_when=("you need to explain an implementation; read a candidate file next",),
                returns=("unverified candidate paths; inspect coverage and read each candidate at the requested immutable ref",),
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
            return ToolResult(
                call.call_id, False,
                data=dict(error.diagnostics) or None,
                error_code=error.code,
                message=error.message,
                hint=error.hint,
                retryable=error.retryable,
                recovery_kind=error.recovery_kind,
                recovery_action=dict(error.recovery_action),
                meta={"untrusted_data": True},
            )
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
        if response.status_code in {429, 502, 503, 504}:
            raise _upstream_response_failure(
                "RATE_LIMITED" if response.status_code == 429 else "NETWORK_ERROR",
                "GitHub service is temporarily unavailable",
                endpoint, response, retryable=True,
            )
        if response.status_code == 403 and _is_rate_limited(response):
            # GitHub answers an exhausted unauthenticated quota with 403, not
            # 429. Without a token only 60 requests/hour are available, so the
            # operator needs to be told the actionable fix rather than "HTTP 403".
            raise _upstream_response_failure(
                "RATE_LIMITED",
                "GitHub API rate limit exhausted"
                + (
                    "; configure TSM_AGT_GITHUB_TOKEN to raise the limit"
                    if not self._configuration.token else ""
                ),
                endpoint, response, retryable=True,
            )
        if endpoint.startswith("/search/code?") and response.status_code in {401, 403}:
            raise _upstream_response_failure(
                "AUTH_REQUIRED",
                "GitHub code search requires an authenticated GitHub token; configure "
                "TSM_AGT_GITHUB_TOKEN or sign in with gh auth login",
                endpoint,
                response,
            )
        if response.status_code >= 400:
            raise _upstream_response_failure(
                "NOT_FOUND" if response.status_code == 404 else "GITHUB_HTTP_ERROR",
                f"GitHub returned HTTP {response.status_code}", endpoint, response,
            )
        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError as error:
            raise _upstream_response_failure(
                "UPSTREAM_PROTOCOL_ERROR",
                "GitHub API returned a non-JSON response where a JSON object was required",
                endpoint, response, retryable=True,
                expected_shape="json_object", received_shape="invalid_json",
            ) from error
        if not isinstance(payload, Mapping):
            raise _upstream_response_failure(
                "UPSTREAM_PROTOCOL_ERROR",
                "GitHub API returned a JSON value other than the required object",
                endpoint, response, retryable=True,
                expected_shape="json_object",
                received_shape=type(payload).__name__,
            )
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
        """Discover candidates through GitHub's code-search service.

        This intentionally does *not* emulate a repository-wide grep by
        downloading an arbitrary prefix of the tree and fetching blobs one by
        one.  That approach conflates a request budget with search coverage:
        an empty result from its first few blobs looks successful even when it
        says nothing about the rest of the repository.

        GitHub's search endpoint is the remote equivalent of the indexed search
        used by coding agents such as Codex and Hermes.  Its pagination and
        ``incomplete_results`` signal are represented directly below.  Search
        results are candidates only because GitHub indexes the default branch;
        callers must read a candidate at ``sha`` before making a source claim.
        """
        needle = query.strip().strip('"').strip("'").strip() or query.strip()
        page, per = self._page(args, _MAX_SEARCH_RESULTS)
        terms = [needle, "in:file", f"repo:{repo}"]
        if path:
            terms.append(f"path:{path}")
        endpoint = "/search/code?" + urlencode({
            "q": " ".join(terms),
            "page": page,
            "per_page": per,
        })
        payload = self._request(endpoint, context)
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise _GitHubFailure("INVALID_RESPONSE", "GitHub code search response lacks an items array")
        try:
            total_count = int(payload.get("total_count", 0))
        except (TypeError, ValueError) as error:
            raise _GitHubFailure("INVALID_RESPONSE", "GitHub code search response has an invalid total_count") from error
        if total_count < 0:
            raise _GitHubFailure("INVALID_RESPONSE", "GitHub code search response has a negative total_count")

        candidates: list[dict[str, str]] = []
        malformed_items = 0
        for item in raw_items:
            if not isinstance(item, Mapping):
                malformed_items += 1
                continue
            raw_path = item.get("path")
            raw_blob_sha = item.get("sha")
            if not isinstance(raw_path, str) or not raw_path.strip() or not isinstance(raw_blob_sha, str):
                malformed_items += 1
                continue
            try:
                candidate_path = self._path(raw_path)
            except ValueError:
                malformed_items += 1
                continue
            candidates.append({
                "path": candidate_path,
                "type": "blob",
                # This is the index result's blob identity, not an assertion
                # that it belongs to the requested commit. get_file is the
                # pinning/verification operation.
                "blob_sha": raw_blob_sha,
                "url": self._web_url(repo, sha, candidate_path),
            })

        index_incomplete = bool(payload.get("incomplete_results"))
        # GitHub exposes at most 1,000 code-search results.  A page can be
        # complete while the overall result set remains unavailable beyond that
        # endpoint cap, so expose both facts rather than a misleading boolean.
        reachable_total = min(total_count, 1_000)
        page_complete = page * per >= reachable_total
        result_cap_reached = total_count > 1_000
        complete = not index_incomplete and page_complete and not result_cap_reached and not malformed_items
        truncated = not complete
        next_page = page + 1 if page * per < reachable_total else None
        status = "matches" if candidates else ("partial" if truncated else "no_match")
        continuation = (
            {"tool": "source.github.search_code", "arguments": {
                "repo": repo, "ref": sha, "query": needle,
                **({"path": path} if path else {}),
                "page": next_page, "per_page": per,
            }}
            if next_page is not None else None
        )
        coverage = {
            "backend": "github_code_search",
            "scope": "github_default_branch_index",
            "requested_commit": sha,
            "requested_commit_verified": False,
            "index_query_complete": not index_incomplete,
            "result_page_complete": page_complete,
            "result_cap_reached": result_cap_reached,
            "malformed_items_skipped": malformed_items,
        }
        extra: dict[str, Any] = {
            "query": needle,
            "search_status": status,
            "total_count": total_count,
            "returned_count": len(candidates),
            "next_page": next_page,
            "continuation": continuation,
            "complete": complete,
            "coverage": coverage,
        }
        if candidates:
            extra["hint"] = (
                "These are unverified index candidates. Read a returned path with "
                "source.github.get_file at the requested commit before describing "
                "what the source does."
            )
        elif truncated:
            extra["hint"] = (
                "GitHub returned an incomplete or bounded search result. This is "
                "not a no-match; follow continuation when present or narrow the "
                "query/path."
            )
        else:
            extra["hint"] = (
                "No candidates were found in GitHub's default-branch search index. "
                "That does not prove an arbitrary requested commit lacks this text."
            )
        return self._source(repo, sha, path or "", None, candidates, truncated, extra=extra)
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
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    hint: str | None = None
    recovery_action: Mapping[str, Any] = field(default_factory=dict)


def _upstream_response_failure(
    code: str,
    message: str,
    endpoint: str,
    response: GitHubApiResponse,
    *,
    retryable: bool = False,
    expected_shape: str | None = None,
    received_shape: str | None = None,
) -> _GitHubFailure:
    """Return bounded upstream facts without treating response text as trusted."""

    upstream: dict[str, Any] = {
        "endpoint": _diagnostic_endpoint(endpoint),
        "http_status": response.status_code,
        "content_type": _content_type(response.headers),
        "body_bytes": len(response.body),
        "body_sha256": "sha256:" + hashlib.sha256(response.body).hexdigest(),
        "body_preview": _response_preview(response.body),
    }
    if expected_shape is not None:
        upstream["expected_shape"] = expected_shape
    if received_shape is not None:
        upstream["received_shape"] = received_shape
    return _GitHubFailure(
        code,
        message,
        retryable=retryable,
        recovery_kind=(ToolRecoveryKind.RETRY_SAME if retryable else ToolRecoveryKind.NONE),
        diagnostics={"upstream": upstream},
        hint=(
            "The same read-only request is safe to retry. If this upstream "
            "response repeats, use another visible source path; do not treat "
            "it as repository evidence."
            if retryable else None
        ),
        recovery_action=(
            {"action": "retry_same", "same_call_safe": True,
             "reason": "upstream_response"}
            if retryable else {}
        ),
    )


def _diagnostic_endpoint(endpoint: str) -> str:
    """Keep the request path, never query values, in durable diagnostics."""

    path = urlsplit(endpoint).path
    return path if path.startswith("/") else "/" + path


def _content_type(headers: Mapping[str, str]) -> str | None:
    for key, value in headers.items():
        if str(key).casefold() == "content-type":
            return sanitize_historical_error_text(str(value))[:160]
    return None


def _response_preview(body: bytes) -> str:
    """Return a short, redacted diagnostic lead, never a response payload."""

    text = body.decode("utf-8", errors="replace")
    printable = " ".join(text.split())
    return sanitize_historical_error_text(printable)[:256]
