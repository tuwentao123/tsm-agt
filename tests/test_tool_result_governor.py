import json
import unittest
from collections import OrderedDict

from tsm_agt.core.tool_result_governor import DefaultToolResultGovernor
from tsm_agt.ports import (
    ToolCall,
    ToolFactDescriptor,
    ToolRecoveryKind,
    ToolResult,
    ToolRisk,
    ToolSpec,
)


def _spec(
    name: str = "fixture.governed", *, max_result_tokens: int = 64, requires_network: bool = False
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Return a result for governor tests.",
        parameters={"type": "object", "additionalProperties": True},
        risk=ToolRisk.R0,
        max_result_tokens=max_result_tokens,
        requires_network=requires_network,
    )


class ToolResultGovernorTest(unittest.TestCase):
    def test_recursively_redacts_sensitive_values_and_marks_external_content(self) -> None:
        result = ToolResult(
            call_id="call-1",
            ok=True,
            data={
                "nested": {"access_token": "secret-token"},
                "url": "https://example.test/path?token=secret-token&safe=value",
                "authorization": "Bearer secret-token",
            },
            message="request used Bearer secret-token",
            meta={"headers": {"Cookie": "session-secret"}},
            fact_descriptor=ToolFactDescriptor(
                authority="external", scope=("example.test",), content_hash="sha256:fact"
            ),
        )

        governed = DefaultToolResultGovernor().govern(
            _spec(requires_network=True), ToolCall("call-1", "fixture.governed"), result
        )

        self.assertEqual(governed.data["nested"]["access_token"], "[REDACTED]")
        self.assertEqual(governed.data["authorization"], "[REDACTED]")
        self.assertIn("token=%5BREDACTED%5D", governed.data["url"])
        self.assertEqual(governed.meta["headers"]["Cookie"], "[REDACTED]")
        self.assertIn("Bearer [REDACTED]", governed.message)
        self.assertTrue(governed.meta["untrusted_data"])
        self.assertTrue(governed.meta["result_governance"]["redacted"])
        self.assertEqual(governed.fact_descriptor, result.fact_descriptor)
        self.assertNotIn("secret-token", json.dumps(governed.to_data()))

    def test_does_not_compact_content_and_preserves_containers_and_identity(self) -> None:
        """Only redaction may change a payload; size limits belong to the tool.

        This governor used to collapse an over-budget list to its first and last
        element. That turned a 31-entry workspace listing into two files
        (``task-c4dfa243``) while ``truncated`` stayed false.
        """

        matches = [
            {"path": f"src/file-{index}.py", "line": index, "text": "x" * 120}
            for index in range(4)
        ]
        result = ToolResult(
            call_id="call-1",
            ok=False,
            data=OrderedDict(
                [
                    ("path", "src/main.py"),
                    ("sha256", "sha256:stable"),
                    ("matches", matches),
                ]
            ),
            error_code="TOOL_FAILED",
            message="m" * 120,
            hint="h" * 120,
            retryable=True,
            recovery_kind=ToolRecoveryKind.RETRY_SAME,
            recovery_action={"retry": "exact-action"},
            meta=OrderedDict([("page", 3), ("provider", "fixture")]),
        )

        governed = DefaultToolResultGovernor().govern(
            _spec("core.search_text", max_result_tokens=20),
            ToolCall("call-1", "core.search_text"),
            result,
        )

        summary = governed.meta["result_governance"]
        self.assertIsInstance(governed.data, OrderedDict)
        self.assertIsInstance(governed.meta, OrderedDict)
        self.assertEqual(governed.data["path"], "src/main.py")
        self.assertEqual(governed.data["sha256"], "sha256:stable")
        self.assertIsInstance(governed.data["matches"], list)
        self.assertEqual(
            [match["path"] for match in governed.data["matches"]],
            [f"src/file-{index}.py" for index in range(4)],
            "every item survives, in order",
        )
        self.assertEqual(governed.message, "m" * 120)
        self.assertEqual(governed.hint, "h" * 120)
        self.assertNotIn("governor_truncated", summary)
        self.assertNotIn("omitted_items", summary)
        self.assertFalse(summary["provider_truncated"])
        self.assertFalse(governed.truncated)
        self.assertEqual(governed.recovery_action, result.recovery_action)

    def test_preserves_profiles_for_web_and_github_source_results(self) -> None:
        governor = DefaultToolResultGovernor()
        cases = (
            (
                "web.open",
                {
                    "document_ref": "doc-1",
                    "canonical_url": "https://example.test/article",
                    "content_hash": "sha256:document",
                    "provenance": {"source": "web", "status_code": 200},
                    "content": "w" * 300,
                },
                "web_open_find",
            ),
            (
                "source.github.get_file",
                {
                    "repo": "owner/repo",
                    "ref": "a" * 40,
                    "path": "src/main.py",
                    "blob_sha": "b" * 40,
                    "content_hash": "sha256:github",
                    "content": "g" * 300,
                },
                "github_resolve_source",
            ),
        )
        for name, data, profile in cases:
            with self.subTest(name=name):
                governed = governor.govern(
                    _spec(name, max_result_tokens=16), ToolCall("call-1", name),
                    ToolResult(call_id="call-1", ok=True, data=data),
                )
                self.assertEqual(governed.meta["result_governance"]["profile"], profile)
                for key in set(data) - {"content"}:
                    self.assertEqual(governed.data[key], data[key])
                self.assertEqual(governed.data["content"], data["content"])

    def test_preserves_failure_and_unknown_outcome_semantics(self) -> None:
        recovery_action = {"kind": "inspect", "token": "must-not-change"}
        result = ToolResult(
            call_id="call-1",
            ok=False,
            error_code="UNKNOWN_OUTCOME",
            message="Bearer secret-token",
            retryable=False,
            recovery_kind=ToolRecoveryKind.UNKNOWN_OUTCOME,
            recovery_action=recovery_action,
        )

        governed = DefaultToolResultGovernor().govern(
            _spec(), ToolCall("call-1", "fixture.governed"), result
        )

        self.assertEqual(governed.call_id, result.call_id)
        self.assertFalse(governed.ok)
        self.assertEqual(governed.error_code, "UNKNOWN_OUTCOME")
        self.assertFalse(governed.retryable)
        self.assertEqual(governed.recovery_kind, ToolRecoveryKind.UNKNOWN_OUTCOME)
        self.assertIs(governed.recovery_action, recovery_action)

    def test_reports_provider_truncation_without_claiming_its_own(self) -> None:
        result = ToolResult(
            call_id="call-1",
            ok=True,
            data={"path": "src/main.py", "content": "short"},
            truncated=True,
        )

        governed = DefaultToolResultGovernor().govern(
            _spec("core.read_file", max_result_tokens=64),
            ToolCall("call-1", "core.read_file"),
            result,
        )

        summary = governed.meta["result_governance"]
        self.assertTrue(governed.truncated, "the tool's own flag still passes through")
        self.assertTrue(summary["provider_truncated"])
        self.assertNotIn("governor_truncated", summary)
