from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tsm_agt.adapters.github_source import GitHubApiResponse, GitHubSourceToolProvider
from tsm_agt.adapters.github_source.provider import _GitHubFailure
from tsm_agt.adapters.github_source.configuration import GitHubSourceConfiguration, load_github_source_configuration
from tsm_agt.ports import (
    AdapterContext, ToolCall, ToolInvocationContext, ToolRecoveryKind, ToolRisk,
)


class _Transport:
    def __init__(self, responses): self.responses, self.calls = responses, []
    def get(self, url, *, headers, timeout_seconds, max_bytes):
        self.calls.append((url, dict(headers)))
        payload = self.responses[url]
        if isinstance(payload, Exception):
            # The real transport enforces the response byte cap itself; a fake
            # that returns the bytes would not exercise that path.
            raise payload
        return GitHubApiResponse(payload[0], url, payload[1], payload[2])


def _response(payload, status=200, headers=None):
    import json
    return status, headers or {}, json.dumps(payload).encode()


class _CappingTransport:
    """Fake transport that, like the real one, enforces the byte cap."""

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, *, headers, timeout_seconds, max_bytes):
        self.calls.append((url, max_bytes))
        raw = self.responses[url]
        if isinstance(raw, tuple):
            status, response_headers, body = raw
        else:
            status, response_headers, body = 200, {}, json.dumps(raw).encode()
        if len(body) > max_bytes:
            raise _GitHubFailure(
                "RESPONSE_TOO_LARGE",
                f"GitHub response exceeded the {max_bytes}-byte cap for {url}",
            )
        return GitHubApiResponse(status, url, response_headers, body)


class GitHubSourceProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = "https://api.github.com"
        self.repo = "owner/repo"
        self.sha = "a" * 40
        self.transport = _Transport({
            f"{self.base}/repos/{self.repo}/commits/main": _response({"sha": self.sha}, headers={"ETag":"commit-main"}),
            f"{self.base}/repos/{self.repo}/commits/{self.sha}": _response({"sha": self.sha, "html_url":f"https://github.com/{self.repo}/commit/{self.sha}"}),
            f"{self.base}/repos/{self.repo}/contents/src/a.py?ref={self.sha}": _response({"sha":"b"*40,"content":"cHJpbnQoJ29rJykK"}),
        })
        self.provider = GitHubSourceToolProvider(GitHubSourceConfiguration(enabled=True), transport=self.transport)
        await self.provider.start(AdapterContext({}, lambda *_: None))
    async def asyncTearDown(self): await self.provider.stop(datetime.now(timezone.utc))
    def _context(self): return ToolInvocationContext("i", "task", "turn", Path.cwd(), datetime.now(timezone.utc) + timedelta(seconds=10))

    async def test_file_resolves_mutable_ref_and_never_exposes_token(self):
        result = await self.provider.invoke(ToolCall("c", "source.github.get_file", {"repo":self.repo,"path":"src/a.py","ref":"main"}), self._context())
        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["ref"], self.sha)
        self.assertEqual(result.data["blob_sha"], "b" * 40)
        self.assertEqual(result.data["url"], f"https://github.com/{self.repo}/blob/{self.sha}/src/a.py")
        self.assertEqual(result.data["content_hash"], "sha256:" + hashlib.sha256(b"print('ok')\n").hexdigest())
        self.assertEqual(result.fact_descriptor.scope, (self.repo, self.sha, "src/a.py"))
        artifact, = result.fact_descriptor.source_artifacts
        self.assertEqual(artifact.repository, self.repo)
        self.assertEqual(artifact.commit_sha, self.sha)
        self.assertEqual(artifact.blob_sha, "b" * 40)
        self.assertTrue(result.meta["untrusted_data"])
        self.assertNotIn("Authorization", result.to_data().__str__())

    async def test_search_uses_github_code_search_instead_of_scanning_a_tree_prefix(self):
        endpoint = (
            f"{self.base}/search/code?"
            "q=needle+in%3Afile+repo%3Aowner%2Frepo&page=1&per_page=50"
        )
        self.transport.responses[endpoint] = _response({
            "total_count": 2,
            "incomplete_results": False,
            "items": [
                {"path": "src/a.py", "sha": "c" * 40},
                {"path": "src/b.py", "sha": "d" * 40},
            ],
        })

        result = await self.provider.invoke(
            ToolCall(
                "c",
                "source.github.search_code",
                {"repo": self.repo, "ref": "main", "query": "needle"},
            ),
            self._context(),
        )

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual([item["path"] for item in result.data["content"]], ["src/a.py", "src/b.py"])
        self.assertTrue(result.data["complete"])
        self.assertFalse(result.truncated)
        self.assertEqual(result.data["search_status"], "matches")
        self.assertEqual(result.data["coverage"], {
            "backend": "github_code_search",
            "scope": "github_default_branch_index",
            "requested_commit": self.sha,
            "requested_commit_verified": False,
            "index_query_complete": True,
            "result_page_complete": True,
            "result_cap_reached": False,
            "malformed_items_skipped": 0,
        })
        self.assertEqual(
            [url for url, _headers in self.transport.calls],
            [f"{self.base}/repos/{self.repo}/commits/main", endpoint],
        )

    async def test_search_exposes_pagination_as_a_structured_continuation(self):
        endpoint = (
            f"{self.base}/search/code?"
            "q=needle+in%3Afile+repo%3Aowner%2Frepo&page=1&per_page=1"
        )
        self.transport.responses[endpoint] = _response({
            "total_count": 2,
            "incomplete_results": False,
            "items": [{"path": "src/a.py", "sha": "c" * 40}],
        })

        result = await self.provider.invoke(
            ToolCall(
                "c",
                "source.github.search_code",
                {"repo": self.repo, "ref": "main", "query": "needle", "per_page": 1},
            ),
            self._context(),
        )

        self.assertTrue(result.ok, result.to_data())
        self.assertFalse(result.data["complete"])
        self.assertTrue(result.truncated)
        self.assertEqual(result.data["next_page"], 2)
        self.assertEqual(result.data["continuation"], {
            "tool": "source.github.search_code",
            "arguments": {
                "repo": self.repo,
                "ref": self.sha,
                "query": "needle",
                "page": 2,
                "per_page": 1,
            },
        })

    async def test_search_marks_an_incomplete_index_response_as_non_terminal(self):
        endpoint = (
            f"{self.base}/search/code?"
            "q=needle+in%3Afile+repo%3Aowner%2Frepo&page=1&per_page=50"
        )
        self.transport.responses[endpoint] = _response({
            "total_count": 0,
            "incomplete_results": True,
            "items": [],
        })

        result = await self.provider.invoke(
            ToolCall(
                "c",
                "source.github.search_code",
                {"repo": self.repo, "ref": "main", "query": "needle"},
            ),
            self._context(),
        )

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["search_status"], "partial")
        self.assertFalse(result.data["complete"])
        self.assertTrue(result.truncated)
        self.assertIn("not a no-match", result.data["hint"])

    async def test_search_requires_authentication_when_github_rejects_the_index_query(self):
        endpoint = (
            f"{self.base}/search/code?"
            "q=needle+in%3Afile+repo%3Aowner%2Frepo&page=1&per_page=50"
        )
        self.transport.responses[endpoint] = _response(
            {"message": "Requires authentication"}, status=401,
        )

        result = await self.provider.invoke(
            ToolCall(
                "c",
                "source.github.search_code",
                {"repo": self.repo, "ref": "main", "query": "needle"},
            ),
            self._context(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "AUTH_REQUIRED")
        self.assertIn("gh auth login", result.message)

    async def test_rejects_traversal_before_egress(self):
        result = await self.provider.invoke(ToolCall("c", "source.github.get_file", {"repo":self.repo,"path":"../secret","ref":"main"}), self._context())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")
        self.assertEqual(self.transport.calls, [])

    async def test_exhausted_quota_is_reported_as_rate_limited(self):
        # GitHub answers an exhausted unauthenticated quota with 403, not 429.
        self.transport.responses[
            f"{self.base}/repos/{self.repo}/commits/main"
        ] = (
            403,
            {"X-RateLimit-Remaining": "0"},
            json.dumps({"message": "API rate limit exceeded"}).encode(),
        )

        result = await self.provider.invoke(
            ToolCall(
                "c",
                "source.github.get_file",
                {"repo": self.repo, "path": "src/a.py", "ref": "main"},
            ),
            self._context(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "RATE_LIMITED")
        self.assertTrue(result.retryable)
        self.assertIn("TSM_AGT_GITHUB_TOKEN", result.message)

    async def test_non_json_success_response_is_retryable_and_observable(self):
        endpoint = f"{self.base}/repos/{self.repo}/commits/main"
        body = b"<html>upstream gateway failed: Bearer ghp_secret_value token=private</html>"
        self.transport.responses[endpoint] = (
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            body,
        )

        result = await self.provider.invoke(
            ToolCall(
                "c", "source.github.get_file",
                {"repo": self.repo, "path": "src/a.py", "ref": "main"},
            ),
            self._context(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "UPSTREAM_PROTOCOL_ERROR")
        self.assertTrue(result.retryable)
        self.assertEqual(result.recovery_kind, ToolRecoveryKind.RETRY_SAME)
        self.assertEqual(result.data["upstream"]["endpoint"], "/repos/owner/repo/commits/main")
        self.assertEqual(result.data["upstream"]["http_status"], 200)
        self.assertEqual(result.data["upstream"]["content_type"], "text/html; charset=utf-8")
        self.assertEqual(result.data["upstream"]["body_bytes"], len(body))
        self.assertEqual(result.data["upstream"]["expected_shape"], "json_object")
        self.assertEqual(result.data["upstream"]["received_shape"], "invalid_json")
        self.assertIn("[REDACTED]", result.data["upstream"]["body_preview"])
        self.assertNotIn("ghp_secret_value", str(result.to_data()))
        self.assertNotIn("private", str(result.to_data()))
        model_data = result.to_model_data()
        self.assertEqual(model_data["data"]["upstream"]["http_status"], 200)
        self.assertEqual(model_data["data"]["upstream"]["received_shape"], "invalid_json")
        self.assertNotIn("retryable", model_data)

    async def test_blank_optional_path_means_no_filter(self):
        endpoint = f"{self.base}/repos/{self.repo}/git/trees/{self.sha}?recursive=1"
        self.transport.responses[endpoint] = _response({"tree": [
            {"path": "src/a.py", "type": "blob", "sha": "b" * 40},
            {"path": "README.md", "type": "blob", "sha": "d" * 40},
        ]})
        result = await self.provider.invoke(
            ToolCall(
                "c",
                "source.github.list_tree",
                {"repo": self.repo, "ref": "main", "path": ""},
            ),
            self._context(),
        )
        # path is optional in the schema, and a model commonly expresses "no
        # filter" as an empty string. That must list the whole tree, not fail.
        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(
            [item["path"] for item in result.data["content"]],
            ["src/a.py", "README.md"],
        )

    async def test_large_commit_body_does_not_block_tree_resolution(self):
        # A commit that touches many files carries every patch in its body, so
        # it can exceed the content cap even though only its sha is wanted.
        big_commit = {
            "sha": self.sha,
            "files": [
                {"filename": f"src/file_{index}.py", "patch": "x" * 2_000}
                for index in range(400)
            ],
        }
        import json
        self.assertGreater(len(json.dumps(big_commit).encode()), 512_000)
        transport = _CappingTransport({
            f"{self.base}/repos/{self.repo}/commits/HEAD": big_commit,
            f"{self.base}/repos/{self.repo}/git/trees/{self.sha}?recursive=1": _response({
                "tree": [{"path": "src/a.py", "type": "blob", "sha": "b" * 40}],
                "truncated": False,
            }),
        })
        provider = GitHubSourceToolProvider(
            GitHubSourceConfiguration(enabled=True), transport=transport
        )
        await provider.start(AdapterContext({}, lambda *_: None))
        try:
            result = await provider.invoke(
                ToolCall(
                    "c", "source.github.list_tree",
                    {"repo": self.repo, "ref": "HEAD"},
                ),
                self._context(),
            )
        finally:
            await provider.stop(datetime.now(timezone.utc))

        self.assertTrue(result.ok, result.to_data())
        self.assertEqual(result.data["ref"], self.sha)
        resolve_caps = [
            cap for url, cap in transport.calls if url.endswith("/commits/HEAD")
        ]
        # The resolver was allowed the metadata budget, not the content cap.
        self.assertTrue(resolve_caps)
        self.assertGreater(resolve_caps[0], 512_000)

    async def test_etag_304_uses_cached_payload(self):
        first = await self.provider.invoke(ToolCall("one", "source.github.get_commit", {"repo":self.repo,"sha":"main"}), self._context())
        self.assertTrue(first.ok)
        endpoint = f"{self.base}/repos/{self.repo}/commits/main"
        self.transport.responses[endpoint] = (304, {}, b"")
        second = await self.provider.invoke(ToolCall("two", "source.github.get_commit", {"repo":self.repo,"sha":"main"}), self._context())
        self.assertTrue(second.ok, second.to_data())
        self.assertIn("If-None-Match", self.transport.calls[-2][1])

    async def test_private_configuration_is_r1_and_allowlisted(self):
        provider = GitHubSourceToolProvider(GitHubSourceConfiguration(enabled=True, token="github_pat_example", private_access=True, allowed_repositories=(self.repo,)), transport=self.transport)
        await provider.start(AdapterContext({}, lambda *_: None))
        try:
            tools = await provider.list_tools()
            self.assertTrue(all(tool.risk is ToolRisk.R1 for tool in tools))
            denied = await provider.invoke(ToolCall("denied", "source.github.get_commit", {"repo":"other/repo","sha":"main"}), self._context())
            self.assertEqual(denied.error_code, "REPOSITORY_NOT_ALLOWED")
            self.assertNotIn("github_pat_example", denied.to_data().__str__())
        finally: await provider.stop(datetime.now(timezone.utc))


class GitHubSourceConfigurationTest(unittest.TestCase):
    def test_private_access_requires_token_and_exact_repository_allowlist(self):
        configuration = load_github_source_configuration(Path("/missing.env"), {"TSM_AGT_GITHUB_SOURCE_ENABLED":"true", "TSM_AGT_GITHUB_PRIVATE_ACCESS":"true"})
        self.assertFalse(configuration.enabled)
        self.assertTrue(configuration.issues)

    def test_token_source_is_redacted_from_snapshot(self):
        configuration = load_github_source_configuration(Path("/missing.env"), {"TSM_AGT_GITHUB_TOKEN":"github_pat_example"})
        self.assertEqual(configuration.sources["github_source.token"], "environment")
        self.assertNotIn("github_pat_example", str(configuration.sources))

    def test_enabling_the_adapter_adopts_an_existing_gh_login(self):
        # Shell agents never hit GitHub's 60 requests/hour anonymous ceiling
        # because they run inside the developer's authenticated shell. The
        # adapter adopts that same credential instead of demanding a second one.
        adopted = load_github_source_configuration(
            Path("/missing.env"),
            {"TSM_AGT_GITHUB_SOURCE_ENABLED": "true"},
            cli_token_resolver=lambda: "gh" * 20,
        )
        self.assertEqual(adopted.sources["github_source.token"], "gh_cli")
        self.assertEqual(len(adopted.token), 40)

    def test_a_missing_gh_login_leaves_the_adapter_anonymous(self):
        configuration = load_github_source_configuration(
            Path("/missing.env"),
            {"TSM_AGT_GITHUB_SOURCE_ENABLED": "true"},
            cli_token_resolver=lambda: "",
        )
        self.assertTrue(configuration.enabled)
        self.assertEqual(configuration.sources["github_source.token"], "unset")
        self.assertEqual(configuration.token, "")

    def test_an_explicit_token_wins_over_the_gh_login(self):
        configuration = load_github_source_configuration(
            Path("/missing.env"),
            {
                "TSM_AGT_GITHUB_SOURCE_ENABLED": "true",
                "TSM_AGT_GITHUB_TOKEN": "github_pat_example",
            },
            cli_token_resolver=lambda: "gh" * 20,
        )
        self.assertEqual(configuration.sources["github_source.token"], "environment")
        self.assertEqual(configuration.token, "github_pat_example")
