from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    ApprovalRequired, CoreToolPolicy, PolicyAction, ProjectTrustLevel, TaskState,
)
from tsm_agt.ports import RuntimeStorePort, ToolCall, ToolRisk


def command_spec():
    return next(
        spec for spec in CoreProcessToolProvider._tools
        if spec.name == "core.run_command"
    )


class CommandRiskPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = CoreToolPolicy()
        self.spec = command_spec()

    def decision(self, argv: list[str]):
        return self.policy.evaluate(
            self.spec, ToolCall("call-risk", "core.run_command", {"argv": argv})
        )

    def test_local_build_and_test_commands_remain_r2(self) -> None:
        for argv in (
            ["python3", "-m", "pytest"],
            ["./gradlew", "test"],
            ["gradlew.bat", "test"],
            ["npm", "test"],
            ["git", "status"],
        ):
            with self.subTest(argv=argv):
                decision = self.decision(argv)
                self.assertEqual(decision.effective_risk, ToolRisk.R2)
                self.assertEqual(decision.action, PolicyAction.REQUIRE_APPROVAL)
                self.assertFalse(decision.requires_network)

    def test_curl_still_requires_network_approval(self) -> None:
        for url in ("http://localhost:5176/dashboard", "https://example.test"):
            with self.subTest(url=url):
                decision = self.decision(["curl", url])
                self.assertEqual(decision.effective_risk, ToolRisk.R3)
                self.assertEqual(decision.action, PolicyAction.REQUIRE_APPROVAL)
                self.assertTrue(decision.requires_network)

    def test_reading_a_public_repository_needs_no_approval(self) -> None:
        for argv in (
            ["git", "clone", "https://github.com/openai/codex"],
            ["git", "clone", "--depth", "1", "https://github.com/openai/codex", "vendor/codex"],
            ["git", "-C", ".", "fetch", "origin"],
            ["git", "ls-remote", "https://github.com/openai/codex", "HEAD"],
        ):
            with self.subTest(argv=argv):
                decision = self.policy.evaluate(
                    self.spec,
                    ToolCall("call-fetch", "core.run_command", {"argv": argv}),
                    ProjectTrustLevel.TRUSTED_BUILD,
                )
                self.assertEqual(decision.action, PolicyAction.ALLOW)
                self.assertIn("read-only source retrieval", decision.reason)

    def test_config_or_program_injection_keeps_approval(self) -> None:
        for argv in (
            ["git", "clone", "--config=core.fsmonitor=/evil", "https://github.com/openai/codex"],
            ["git", "clone", "--upload-pack", "/evil", "https://github.com/openai/codex"],
            ["git", "clone", "--template", "/evil", "https://github.com/openai/codex"],
            ["git", "clone", "--separate-git-dir=/elsewhere", "https://github.com/openai/codex"],
        ):
            with self.subTest(argv=argv):
                decision = self.policy.evaluate(
                    self.spec,
                    ToolCall("call-inject", "core.run_command", {"argv": argv}),
                    ProjectTrustLevel.TRUSTED_BUILD,
                )
                self.assertNotEqual(decision.action, PolicyAction.ALLOW)

    def test_remote_writes_never_borrow_the_read_only_exemption(self) -> None:
        for argv in (
            ["git", "push", "origin", "main"],
            ["git", "reset", "--hard"],
            ["git", "clean", "-fd"],
            ["git", "pull"],
            ["npm", "publish"],
        ):
            with self.subTest(argv=argv):
                decision = self.policy.evaluate(
                    self.spec,
                    ToolCall("call-write", "core.run_command", {"argv": argv}),
                    ProjectTrustLevel.TRUSTED_BUILD,
                )
                self.assertNotEqual(decision.action, PolicyAction.ALLOW)

    def test_reading_github_with_gh_needs_no_approval(self) -> None:
        for argv in (
            ["gh", "api", "-H", "Accept: application/vnd.github.raw",
             "repos/openai/codex/contents/README.md"],
            ["gh", "api", "repos/openai/codex/git/trees/main?recursive=1",
             "--jq", ".tree[].path"],
            ["gh", "search", "code", "agent loop"],
            ["gh", "auth", "status"],
            ["gh", "repo", "view", "openai/codex"],
        ):
            with self.subTest(argv=argv):
                decision = self.policy.evaluate(
                    self.spec,
                    ToolCall("call-gh-read", "core.run_command", {"argv": argv}),
                    ProjectTrustLevel.TRUSTED_BUILD,
                )
                self.assertEqual(decision.action, PolicyAction.ALLOW)

    def test_a_shell_is_denied_outright_not_parked_for_approval(self) -> None:
        for argv in (
            ["bash", "-lc", "gh repo list"],
            ["sh", "-c", "ls"],
            ["/bin/zsh", "-lc", "pwd"],
            ["powershell", "-Command", "ls"],
        ):
            with self.subTest(argv=argv):
                decision = self.policy.evaluate(
                    self.spec,
                    ToolCall("call-shell", "core.run_command", {"argv": argv}),
                    ProjectTrustLevel.TRUSTED_BUILD,
                )
                # The sandbox never authorizes a shell, so an approval request
                # would only park the turn; the model must be told to retry.
                self.assertEqual(decision.action, PolicyAction.DENY)
                self.assertIn("shell executable is denied", decision.reason)

    def test_invocation_authority_follows_the_classification(self) -> None:
        from tsm_agt.ports import ToolResultAuthority

        for argv in (
            ["gh", "api", "repos/openai/codex/contents/README.md"],
            ["git", "clone", "https://github.com/openai/codex"],
            ["git", "fetch", "origin"],
        ):
            with self.subTest(argv=argv):
                call = ToolCall("call-auth", "core.run_command", {"argv": argv})
                self.assertTrue(self.policy.is_external_source_read(call))
                self.assertIs(
                    self.policy.execution_result_authority(self.spec, call),
                    ToolResultAuthority.EXTERNAL_SERVICE,
                )

        for argv in (
            ["python3", "-m", "pytest"],
            ["git", "push", "origin", "main"],
            ["gh", "api", "-X", "POST", "repos/x/y/issues"],
        ):
            with self.subTest(argv=argv):
                call = ToolCall("call-auth", "core.run_command", {"argv": argv})
                self.assertFalse(self.policy.is_external_source_read(call))
                self.assertIs(
                    self.policy.execution_result_authority(self.spec, call),
                    ToolResultAuthority.PROCESS_FACT,
                )

    def test_source_retrieval_is_narrower_than_external_access(self) -> None:
        from tsm_agt.ports import ToolCall as Call

        def call(argv):
            return Call("c", "core.run_command", {"argv": argv})

        for argv, retrieves, approval_free in (
            (["gh", "api", "repos/o/r/contents/a.py"], True, True),
            (["git", "clone", "https://github.com/o/r"], True, True),
            (["git", "fetch", "origin"], True, True),
            (["git", "ls-remote", "https://github.com/o/r"], False, True),
            (["gh", "search", "code", "x"], False, True),
            (["gh", "repo", "view", "o/r"], False, True),
            (["python3", "-m", "pytest"], False, False),
        ):
            with self.subTest(argv=argv):
                # Only the calls that bring or read content count as having
                # read a source; the ref listing and the views are a cheaper
                # kind of access that completion must not accept.
                self.assertEqual(
                    self.policy.retrieves_external_source(call(argv)), retrieves
                )
                self.assertEqual(
                    self.policy.is_external_source_read(call(argv)), approval_free
                )

    def test_gh_writes_never_borrow_the_read_only_exemption(self) -> None:
        for argv in (
            ["gh", "api", "-X", "POST", "repos/x/y/issues", "-f", "title=t"],
            ["gh", "api", "-f", "title=t", "repos/x/y/issues"],
            ["gh", "api", "--method=DELETE", "repos/x/y"],
            ["gh", "api", "graphql", "-f", "query=x"],
            ["gh", "auth", "login"],
            ["gh", "repo", "clone", "openai/codex"],
            ["gh", "release", "create", "v1"],
        ):
            with self.subTest(argv=argv):
                decision = self.policy.evaluate(
                    self.spec,
                    ToolCall("call-gh-write", "core.run_command", {"argv": argv}),
                    ProjectTrustLevel.TRUSTED_BUILD,
                )
                self.assertNotEqual(decision.action, PolicyAction.ALLOW)

    def test_untrusted_workspace_still_denies_repository_commands(self) -> None:
        decision = self.policy.evaluate(
            self.spec,
            ToolCall(
                "call-untrusted",
                "core.run_command",
                {"argv": ["git", "clone", "https://github.com/openai/codex"]},
            ),
            ProjectTrustLevel.UNTRUSTED,
        )
        self.assertEqual(decision.action, PolicyAction.DENY)

    def test_dependency_network_and_device_writes_escalate_to_r3(self) -> None:
        cases = (
            (["npm", "install"], True),
            (["npm", "ci"], True),
            (["yarn"], True),
            (["python3", "-m", "pip", "install", "requests"], True),
            (["python3", "-m", "pip", "uninstall", "requests"], False),
            (["git", "pull"], True),
            (["adb", "install", "app.apk"], False),
            (["python3", "download.py", "https://example.test/data"], True),
        )
        for argv, network in cases:
            with self.subTest(argv=argv):
                decision = self.decision(argv)
                self.assertEqual(decision.effective_risk, ToolRisk.R3)
                self.assertEqual(decision.action, PolicyAction.REQUIRE_APPROVAL)
                self.assertEqual(decision.requires_network, network)
                self.assertTrue(decision.risk_factors)

    def test_previously_r4_commands_now_require_r3_approval(self) -> None:
        cases = (
            # A shell is refused outright; see the dedicated test above.
            ["python3", "-c", "print('unclassified')"],
            ["rm", "-rf", "build"],
            ["git", "push", "origin", "main"],
            ["git.exe", "push", "origin", "main"],
            ["npm", "publish"],
            ["./gradlew", "release", "--prod"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                decision = self.decision(argv)
                self.assertEqual(decision.effective_risk, ToolRisk.R3)
                self.assertEqual(
                    decision.action, PolicyAction.REQUIRE_APPROVAL
                )
                self.assertIn("explicit approval", decision.reason)

    def test_legacy_declared_r4_is_mapped_to_r3_approval(self) -> None:
        spec = replace(self.spec, name="fixture.legacy-r4", risk=ToolRisk.R4)
        decision = self.policy.evaluate(
            spec, ToolCall("call-legacy", spec.name, {})
        )
        self.assertEqual(decision.effective_risk, ToolRisk.R3)
        self.assertEqual(decision.action, PolicyAction.REQUIRE_APPROVAL)
        self.assertIn("legacy R4", decision.reason)

    def test_effective_risk_never_drops_below_declared_r2(self) -> None:
        decision = self.decision(["true"] )
        self.assertEqual(decision.effective_risk, ToolRisk.R2)

    def test_payload_hash_binds_dynamic_risk_classification(self) -> None:
        local = ToolCall("call-local", "core.run_command", {
            "argv": ["npm", "test"]
        })
        network = ToolCall("call-network", "core.run_command", {
            "argv": ["npm", "install"]
        })
        self.assertNotEqual(
            self.policy.payload_hash(self.spec, local),
            self.policy.payload_hash(self.spec, network),
        )


class CommandRiskKernelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.application = compose_fixture_application(
            tool_adapters=(CoreProcessToolProvider(),)
        )
        await self.application.registry.start_all()
        await self.application.kernel.set_project_trust(
            Path(self.temporary.name), ProjectTrustLevel.TRUSTED_BUILD
        )
        self.task = await self.application.kernel.create_task(
            "classify commands", Path(self.temporary.name), "task-command-risk"
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
        ):
            self.task = await self.application.kernel.transition_task(
                self.task.task_id, state, state.value
            )

    async def asyncTearDown(self) -> None:
        await self.application.registry.stop_all()
        self.temporary.cleanup()

    async def test_git_push_reaches_exact_r3_approval_boundary(self) -> None:
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-push",
                ToolCall("call-push", "core.run_command", {
                    "argv": ["git", "push", "origin", "main"], "cwd": ".",
                }),
            )
        request = caught.exception.request
        self.assertEqual(request.risk, ToolRisk.R3)
        self.assertEqual(request.network_access, "required")
        self.assertIn('"push","origin","main"]', request.preview)
        self.assertIn('"cwd":"."', request.preview)

    async def test_r3_approval_explains_dynamic_network_risk(self) -> None:
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-install",
                ToolCall("call-install", "core.run_command", {
                    "argv": ["npm", "install"], "cwd": ".",
                }),
            )
        request = caught.exception.request
        self.assertEqual(request.risk, ToolRisk.R3)
        self.assertEqual(request.network_access, "required")
        self.assertIn("transmitted", request.data_transmission)
        events = await self.application.registry.require(RuntimeStorePort).read_events(
            self.task.task_id
        )
        decision = events[-3].payload["decision"]
        self.assertEqual(decision["effective_risk"], "R3")
        self.assertTrue(decision["requires_network"])
        self.assertTrue(decision["risk_factors"])

    async def test_destructive_command_reaches_r3_approval_boundary(self) -> None:
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-delete",
                ToolCall("call-delete", "core.run_command", {
                    "argv": ["rm", "-rf", "build"], "cwd": ".",
                }),
            )
        self.assertEqual(caught.exception.request.risk, ToolRisk.R3)
        self.assertEqual(
            (await self.application.kernel.get_task(self.task.task_id)).state,
            TaskState.AWAITING_APPROVAL,
        )
        events = await self.application.registry.require(
            RuntimeStorePort
        ).read_events(self.task.task_id)
        policy = next(
            event for event in events if event.event_type == "policy.evaluated"
        )
        self.assertEqual(policy.payload["decision"]["effective_risk"], "R3")
        self.assertEqual(
            policy.payload["decision"]["action"], "require_approval"
        )
        self.assertIn(
            "approval.requested", [event.event_type for event in events]
        )


if __name__ == "__main__":
    unittest.main()
