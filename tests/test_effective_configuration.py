from __future__ import annotations

import json
import os
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import (
    compose_fixture_application, compose_local_flow_query_application,
    compose_openai_compatible_readonly_application_from_env,
)
from tsm_agt.core import (
    EffectiveConfigurationSnapshot, ProjectTrustLevel, TaskSnapshot, TaskState,
    canonical_hash,
)
from tsm_agt.cli import _inspect_effective_configuration
from tsm_agt.ports import (
    AdapterDescriptor, HealthState, HealthStatus, ProviderCapabilities,
    ToolIdempotency, ToolRisk, ToolSpec,
)


NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


def build_snapshot(
    *, tools: tuple[ToolSpec, ...] = (), adapter_version: str = "1.0",
    revision: int = 1, policy_version: int = 1,
    adapter_capabilities: frozenset[str] = frozenset({"b", "a"}),
    health: HealthState = HealthState.HEALTHY,
) -> EffectiveConfigurationSnapshot:
    return EffectiveConfigurationSnapshot.build(
        captured_at=NOW,
        model={"provider": "fixture", "credentials_configured": False},
        capabilities=ProviderCapabilities(tools=True, context_window=4096),
        tools=tools,
        adapters=((
            AdapterDescriptor(
                "fixture.adapter", adapter_version, "FixturePort", "1.0",
                adapter_capabilities,
            ),
            HealthStatus(health),
        ),),
        policy={"policy_id": "fixture", "version": policy_version},
        project={"trust": ProjectTrustLevel.UNTRUSTED.value},
        sources={"model.provider": "harness_default"},
        revision=revision,
    )


class EffectiveConfigurationModelTest(unittest.TestCase):
    def test_canonical_hash_is_order_independent(self) -> None:
        self.assertEqual(
            canonical_hash({"b": 2, "a": 1}),
            canonical_hash({"a": 1, "b": 2}),
        )

    def test_tool_adapter_and_policy_changes_change_their_hashes(self) -> None:
        tool = ToolSpec(
            "fixture.read", "read", {"type": "object"}, ToolRisk.R0,
            True, True, ToolIdempotency.IDEMPOTENT,
        )
        changed_tool = ToolSpec(
            "fixture.read", "read", {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            }, ToolRisk.R0,
            True, True, ToolIdempotency.IDEMPOTENT,
        )
        original = build_snapshot(tools=(tool,))
        self.assertNotEqual(
            original.toolset_hash, build_snapshot(tools=(changed_tool,)).toolset_hash
        )
        self.assertNotEqual(
            original.adapter_lock_hash,
            build_snapshot(tools=(tool,), adapter_version="2.0").adapter_lock_hash,
        )
        self.assertNotEqual(
            original.adapter_lock_hash,
            build_snapshot(
                tools=(tool,), adapter_capabilities=frozenset({"a"})
            ).adapter_lock_hash,
        )
        self.assertNotEqual(
            original.adapter_lock_hash,
            build_snapshot(
                tools=(tool,), health=HealthState.DEGRADED
            ).adapter_lock_hash,
        )
        self.assertNotEqual(
            original.policy_hash,
            build_snapshot(tools=(tool,), policy_version=2).policy_hash,
        )

    def test_task_round_trip_and_revision_are_strict(self) -> None:
        task = TaskSnapshot.create("task-1", "goal", "/workspace", NOW)
        task = task.with_effective_configuration(build_snapshot())
        self.assertEqual(TaskSnapshot.from_data(task.to_data()), task)
        with self.assertRaisesRegex(ValueError, "expected 2"):
            task.with_effective_configuration(build_snapshot(revision=3))


class EffectiveConfigurationKernelTest(unittest.IsolatedAsyncioTestCase):
    async def _advance_to_execution(self, application, task_id: str):
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.PLANNING, TaskState.EXECUTING,
        ):
            await application.kernel.transition_task(task_id, state, state.value)

    async def test_snapshot_and_state_event_commit_together(self) -> None:
        application = compose_fixture_application()
        await application.registry.start_all()
        try:
            task = await application.kernel.create_task("goal", Path.cwd())
            with self.assertRaisesRegex(LookupError, "no effective configuration"):
                await application.kernel.get_effective_configuration(task.task_id)
            await self._advance_to_execution(application, task.task_id)
            stored = await application.kernel.get_task(task.task_id)
            self.assertEqual(len(stored.effective_configurations), 1)
            events = await application.kernel.dependencies.store.read_events(task.task_id)
            self.assertEqual(
                [event.event_type for event in events[-2:]],
                ["config.snapshot", "task.state_changed"],
            )
            self.assertEqual(events[-1].sequence, events[-2].sequence + 1)
            snapshot_event = events[-2].payload
            self.assertNotIn("model", snapshot_event)
            self.assertEqual(
                snapshot_event["effective_config_hash"],
                stored.effective_configurations[0].effective_config_hash,
            )
            configuration = stored.effective_configurations[0]
            self.assertEqual(
                configuration.prompt_manifest["status"], "configured"
            )
            self.assertEqual(
                configuration.prompt_manifest_hash,
                configuration.prompt_manifest["manifest_hash"],
            )
            self.assertNotIn(
                "Obey the runtime policy", json.dumps(configuration.to_data())
            )
            self.assertEqual(
                configuration.execution["context"]["trigger_ratio"], 0.80
            )
            self.assertEqual(
                configuration.execution["context"][
                    "latency_soft_input_tokens"
                ], 0,
            )
            with self.assertRaisesRegex(LookupError, "revision 2 not found"):
                await application.kernel.get_effective_configuration(task.task_id, 2)
            await application.kernel.transition_task(
                task.task_id, TaskState.VERIFYING, "verify"
            )
            after = await application.kernel.get_task(task.task_id)
            self.assertEqual(after.effective_configurations, stored.effective_configurations)
            later_events = await application.kernel.dependencies.store.read_events(
                task.task_id
            )
            self.assertEqual(
                sum(event.event_type == "config.snapshot" for event in later_events), 1
            )
        finally:
            await application.registry.stop_all()

    async def test_readonly_query_returns_stored_not_live_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            writer = compose_fixture_application(
                store_adapter=SQLiteRuntimeStore(workspace / ".agent/runtime.db")
            )
            await writer.registry.start_all()
            try:
                task = await writer.kernel.create_task("goal", workspace)
                await self._advance_to_execution(writer, task.task_id)
                expected = await writer.kernel.get_effective_configuration(task.task_id)
            finally:
                await writer.registry.stop_all()
            reader = compose_local_flow_query_application(workspace)
            await reader.registry.start_all()
            try:
                actual = await reader.kernel.get_effective_configuration(task.task_id)
                self.assertEqual(actual, expected)
            finally:
                await reader.registry.stop_all()
            output = io.StringIO()
            with redirect_stdout(output):
                result = await _inspect_effective_configuration(
                    task.task_id, workspace, 1, True
                )
            self.assertEqual(result, 0)
            cli_data = json.loads(output.getvalue())
            self.assertEqual(cli_data["effective_config_hash"], expected.effective_config_hash)

    async def test_env_sources_and_secrets_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TSM_AGT_MODEL": "exported-model"}, clear=True
        ):
            root = Path(directory)
            secret = "sk-super-secret-value"
            env_file = root / ".env"
            env_file.write_text(
                "TSM_AGT_MODEL_BASE_URL=https://user:pass@example.test:8443/v1/chat?token=x#f\n"
                "TSM_AGT_MODEL=file-model\n"
                f"TSM_AGT_MODEL_API_KEY={secret}\n", encoding="utf-8",
            )
            application = compose_openai_compatible_readonly_application_from_env(
                env_file, root / "runtime.db"
            )
            await application.registry.start_all()
            try:
                task = await application.kernel.create_task("goal", root)
                await self._advance_to_execution(application, task.task_id)
                snapshot = await application.kernel.get_effective_configuration(
                    task.task_id
                )
                encoded = json.dumps(snapshot.to_data(), sort_keys=True)
                self.assertNotIn(secret, encoded)
                self.assertNotIn("user:pass", encoded)
                self.assertNotIn("/v1/chat", encoded)
                self.assertEqual(
                    snapshot.model["endpoint_origin"], "https://example.test:8443"
                )
                self.assertEqual(snapshot.model["model"], "exported-model")
                self.assertEqual(snapshot.sources["model.model"], "environment")
                self.assertEqual(
                    snapshot.sources["model.credentials_configured"], "env_file"
                )
                self.assertEqual(
                    snapshot.model["output_token_parameter"], "max_tokens"
                )
                self.assertTrue(snapshot.model["strict_tool_schema"])
            finally:
                await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
