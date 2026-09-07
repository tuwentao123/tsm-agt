from __future__ import annotations

import tempfile
import unittest
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

    def test_shell_inline_code_delete_publish_and_production_are_r4_denied(self) -> None:
        cases = (
            ["bash", "-c", "echo unsafe"],
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
                self.assertEqual(decision.effective_risk, ToolRisk.R4)
                self.assertEqual(decision.action, PolicyAction.DENY)
                self.assertIn("R4", decision.reason)

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

    async def test_r3_approval_explains_dynamic_network_risk(self) -> None:
        with self.assertRaises(ApprovalRequired) as caught:
            await self.application.kernel.invoke_tool(
                self.task.task_id, "turn-install",
                ToolCall("call-install", "core.run_command", {
                    "argv": ["npm", "install"]
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

    async def test_r4_is_denied_before_approval_or_process_execution(self) -> None:
        result = await self.application.kernel.invoke_tool(
            self.task.task_id, "turn-delete",
            ToolCall("call-delete", "core.run_command", {
                "argv": ["rm", "-rf", "build"]
            }),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertEqual((await self.application.kernel.get_task(self.task.task_id)).state, TaskState.EXECUTING)
        events = await self.application.registry.require(RuntimeStorePort).read_events(
            self.task.task_id
        )
        self.assertEqual(events[-2].payload["decision"]["effective_risk"], "R4")
        self.assertEqual(events[-2].payload["decision"]["action"], "deny")
        self.assertEqual(events[-1].event_type, "tool.failed")
        self.assertNotIn("approval.requested", [event.event_type for event in events[-2:]])


if __name__ == "__main__":
    unittest.main()
