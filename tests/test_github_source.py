from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tsm_agt.adapters.github_source import GitHubApiResponse, GitHubSourceToolProvider
from tsm_agt.adapters.github_source.configuration import GitHubSourceConfiguration, load_github_source_configuration
from tsm_agt.ports import AdapterContext, ToolCall, ToolInvocationContext, ToolRisk


class _Transport:
    def __init__(self, responses): self.responses, self.calls = responses, []
    def get(self, url, *, headers, timeout_seconds, max_bytes):
        self.calls.append((url, dict(headers)))
        payload = self.responses[url]
        return GitHubApiResponse(payload[0], url, payload[1], payload[2])


def _response(payload, status=200, headers=None):
    import json
    return status, headers or {}, json.dumps(payload).encode()


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
        fact, = result.evidence_facts
        self.assertEqual(fact.kind, "source.artifact")
        self.assertTrue(fact.complete)
        self.assertEqual(fact.attributes["repository"], self.repo)
        self.assertTrue(fact.attributes["immutable_revision"])
        self.assertTrue(result.meta["untrusted_data"])
        self.assertNotIn("Authorization", result.to_data().__str__())

    async def test_rejects_traversal_before_egress(self):
        result = await self.provider.invoke(ToolCall("c", "source.github.get_file", {"repo":self.repo,"path":"../secret","ref":"main"}), self._context())
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PARAM")
        self.assertEqual(self.transport.calls, [])

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
        self.assertEqual(configuration.sources["github_source.token"], "configured")
        self.assertNotIn("github_pat_example", str(configuration.sources))
