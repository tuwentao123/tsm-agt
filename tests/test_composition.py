from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import (
    compose_local_flow_query_application,
    compose_openai_compatible_engineering_application,
    compose_fixture_application,
    compose_openai_compatible_engineering_application_from_env,
    compose_openai_compatible_readonly_application_from_env,
    compose_readonly_application,
)
from tsm_agt.bootstrap.exploration_configuration import (
    ExplorationBudgetConfiguration,
)
from tsm_agt.core import (
    ApprovalDecision, ApprovalRequired, ProjectTrustLevel, TaskState,
)
from tsm_agt.ports import (
    CrossProcessLockPort,
    EvidenceDeltaEvaluatorPort,
    ReadHitsPolicyPort,
    ArtifactReadPolicyPort,
    ProgressiveScopePolicyPort,
    SemanticActionClassifierPort,
    CodeIntelligencePort,
    FlowArtifactExportPort,
    ReplayCursorStorePort,
    ModelProviderPort,
    ProcessExecutorPort,
    RuntimeStorePort,
    SandboxPort,
    ToolCall,
    ToolProviderPort,
    WorkspaceFilesystemPort,
    WorkspacePathPort,
    LocalIdentityPort,
    ExplorationBudgetPolicyPort,
    EvidenceRelationProviderPort, EvidenceRelationPolicyPort,
    RejectionLoopPolicyPort, ExplorationOutcomePolicyPort,
)


class CompositionTest(unittest.IsolatedAsyncioTestCase):
    async def test_flow_query_composition_exposes_artifact_export_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            writer = compose_fixture_application(
                store_adapter=SQLiteRuntimeStore(
                    workspace / ".agent" / "runtime.db"
                )
            )
            await writer.registry.start_all()
            try:
                await writer.kernel.create_task("fixture", workspace, "task-flow-port")
            finally:
                await writer.registry.stop_all()
            application = compose_local_flow_query_application(workspace)
            await application.registry.start_all()
            try:
                exporter = application.registry.require(FlowArtifactExportPort)
                self.assertIn("no-overwrite", exporter.descriptor.capabilities)
                self.assertIn("private-file", exporter.descriptor.capabilities)
                self.assertIn("html", exporter.descriptor.capabilities)
                self.assertIn("jsonl", exporter.descriptor.capabilities)
                cursor_store = application.registry.require(ReplayCursorStorePort)
                self.assertIn(
                    "atomic-replace", cursor_store.descriptor.capabilities
                )
            finally:
                await application.registry.stop_all()

    async def test_fixture_application_has_all_required_ports(self) -> None:
        application = compose_fixture_application()
        await application.registry.start_all()
        try:
            self.assertEqual(
                application.kernel.dependencies.default_max_output_tokens, 1024
            )
            self.assertIsNotNone(application.registry.require(ModelProviderPort))
            self.assertIsNotNone(application.registry.require(ProcessExecutorPort))
            self.assertIsNotNone(application.registry.require(RuntimeStorePort))
            self.assertIsNotNone(application.registry.require(SandboxPort))
            self.assertIsNotNone(
                application.registry.require(CrossProcessLockPort)
            )
            self.assertIsNotNone(
                application.registry.require(WorkspaceFilesystemPort)
            )
            self.assertIsNotNone(application.registry.require(WorkspacePathPort))
            self.assertIsNotNone(application.registry.require(LocalIdentityPort))
            self.assertEqual(
                application.registry.all(EvidenceDeltaEvaluatorPort), ()
            )
            self.assertEqual(
                application.registry.all(SemanticActionClassifierPort), ()
            )
            self.assertEqual(len(application.registry.all(ToolProviderPort)), 1)
            for port in (
                ModelProviderPort,
                ProcessExecutorPort,
                RuntimeStorePort,
                SandboxPort,
                CrossProcessLockPort,
                WorkspaceFilesystemPort,
                WorkspacePathPort,
                LocalIdentityPort,
                ToolProviderPort,
            ):
                health = await application.registry.require(port).health()
                self.assertEqual(health.state, "healthy")
        finally:
            await application.registry.stop_all()

    async def test_tool_providers_are_optional(self) -> None:
        application = compose_fixture_application(tool_adapters=())
        await application.registry.start_all()
        try:
            self.assertEqual(application.registry.all(ToolProviderPort), ())
            self.assertEqual(await application.kernel.list_tools(), ())
        finally:
            await application.registry.stop_all()

    async def test_readonly_application_exposes_builtin_workspace_tools(self) -> None:
        application = compose_readonly_application()
        await application.registry.start_all()
        try:
            self.assertEqual(
                [tool.name for tool in await application.kernel.list_tools()],
                [
                    "core.list_files", "core.find_files",
                    "core.read_file", "core.search_text",
                ],
            )
        finally:
            await application.registry.stop_all()

    async def test_real_provider_composition_requires_explicit_tsm_agt_variables(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "TSM_AGT_MODEL_BASE_URL"):
                compose_openai_compatible_readonly_application_from_env(
                    Path("/definitely/missing/tsm-agt.env")
                )

    async def test_real_provider_composition_ignores_unrelated_api_keys(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_API_KEY": "must-not-be-used",
                "TSM_AGT_MODEL_BASE_URL": "https://models.example.test/v1",
                "TSM_AGT_MODEL": "test-model",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "TSM_AGT_MODEL_API_KEY"):
                compose_openai_compatible_readonly_application_from_env(
                    Path("/definitely/missing/tsm-agt.env")
                )

    async def test_real_provider_engineering_composition_has_process_tools(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TSM_AGT_MODEL_BASE_URL": "https://models.example.test/v1",
                "TSM_AGT_MODEL": "test-model",
                "TSM_AGT_MODEL_API_KEY": "test-secret",
            },
            clear=True,
        ):
            application = compose_openai_compatible_engineering_application_from_env()
        await application.registry.start_all()
        try:
            self.assertEqual(
                application.kernel.dependencies.default_max_output_tokens, 8192
            )
            model = application.registry.require(ModelProviderPort)
            self.assertTrue(model.capabilities.tools)
            sandbox = application.registry.require(SandboxPort)
            self.assertEqual(
                sandbox.descriptor.adapter_id,
                "builtin.local-workspace-command-gate",
            )
            self.assertNotIn("deny-all", sandbox.descriptor.capabilities)
            self.assertEqual(
                [tool.name for tool in await application.kernel.list_tools()],
                [
                    "core.list_files", "core.find_files",
                    "core.read_file", "core.search_text",
                    "core.run_command",
                    "core.process_status", "core.process_logs",
                    "core.process_stop", "core.apply_patch",
                    "core.apply_patches", "core.delete_file",
                    "core.rollback_mutation",
                    "core.rollback_mutations",
                    "core.rollback_mutation_batch",
                    "core.rollback_mutation_groups",
                    "core.memory_list", "core.memory_remember",
                    "core.memory_verify", "core.memory_forget",
                    "core.working_memory_read",
                    "core.working_memory_update",
                    "core.task_spec_read",
                    "core.task_outcome_select",
                    "core.task_spec_update",
                    "code.symbol_overview", "code.definition",
                    "code.references", "code.implementations",
                    "code.workspace_symbols", "code.diagnostics",
                    "code.rename_preview", "core.request_input",
                ],
            )
            self.assertIsNotNone(
                application.registry.require(CodeIntelligencePort)
            )
            evaluator = application.registry.require(
                EvidenceDeltaEvaluatorPort
            )
            self.assertEqual(
                evaluator.descriptor.adapter_id,
                "builtin.structured-evidence-delta",
            )
            classifier = application.registry.require(
                SemanticActionClassifierPort
            )
            self.assertEqual(
                classifier.descriptor.adapter_id,
                "builtin.rule-based-semantic-action",
            )
            read_hits = application.registry.require(ReadHitsPolicyPort)
            self.assertEqual(
                read_hits.descriptor.adapter_id,
                "builtin.rule-based-read-hits",
            )
            artifact_read = application.registry.require(ArtifactReadPolicyPort)
            self.assertEqual(
                artifact_read.descriptor.adapter_id,
                "builtin.rule-based-artifact-read",
            )
            self.assertEqual(
                len(application.registry.all(EvidenceRelationProviderPort)), 1
            )
            self.assertEqual(
                application.registry.require(
                    EvidenceRelationPolicyPort
                ).descriptor.adapter_id,
                "builtin.rule-based-evidence-relation",
            )
            self.assertEqual(
                application.registry.require(
                    RejectionLoopPolicyPort
                ).descriptor.adapter_id,
                "builtin.bounded-rejection-loop",
            )
            self.assertEqual(
                application.registry.require(
                    ExplorationOutcomePolicyPort
                ).descriptor.adapter_id,
                "builtin.rule-based-exploration-outcome",
            )
        finally:
            await application.registry.stop_all()

    def test_exploration_profile_can_select_legacy_with_one_setting(self) -> None:
        application = compose_openai_compatible_engineering_application(
            base_url="https://models.example.test/v1",
            model="test-model", api_key="test-secret",
            exploration_budget_configuration=ExplorationBudgetConfiguration(
                profile="legacy"
            ),
        )
        self.assertEqual(
            application.registry.require(
                ProgressiveScopePolicyPort
            ).descriptor.adapter_id,
            "builtin.rule-based-progressive-scope",
        )
        self.assertEqual(
            application.registry.all(EvidenceRelationPolicyPort), ()
        )

    def test_unknown_exploration_profile_fails_instead_of_falling_back(self) -> None:
        with self.assertRaisesRegex(ValueError, "balanced.*legacy"):
            ExplorationBudgetConfiguration(profile="mystery")

    async def test_engineering_composition_approved_command_passes_local_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            application = compose_openai_compatible_engineering_application(
                base_url="https://models.example.test/v1",
                model="test-model",
                api_key="test-secret",
                database_path=workspace / "runtime.db",
            )
            await application.registry.start_all()
            try:
                script = workspace / "gate_smoke.py"
                script.write_text("print('gate-ok')\n", encoding="utf-8")
                await application.kernel.set_project_trust(
                    workspace, ProjectTrustLevel.TRUSTED_BUILD
                )
                task = await application.kernel.create_task(
                    "run approved local command", workspace, "task-local-gate"
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await application.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                call = ToolCall(
                    "call-local-gate", "core.run_command",
                    {"argv": [sys.executable, script.name]},
                )
                with self.assertRaises(ApprovalRequired) as caught:
                    await application.kernel.invoke_tool(
                        task.task_id, "turn-local-gate", call
                    )
                request = caught.exception.request
                result = await application.kernel.resolve_approval(
                    task.task_id, request.request_id, request.payload_hash,
                    ApprovalDecision.APPROVE, "approve exact Python smoke command",
                )
                self.assertTrue(result.ok)
                self.assertEqual(result.data["stdout"]["text"], "gate-ok\n")
            finally:
                await application.registry.stop_all()

    async def test_local_env_file_loads_only_explicit_model_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "# local settings\n"
                "TSM_AGT_MODEL_BASE_URL='http://127.0.0.1:5580'\n"
                "export TSM_AGT_MODEL=local-model\n"
                "TSM_AGT_MODEL_API_KEY=local-secret\n"
                "UNRELATED_SECRET=must-not-load\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = compose_openai_compatible_readonly_application_from_env(
                    env_file
                )
                self.assertEqual(os.environ["TSM_AGT_MODEL"], "local-model")
                self.assertNotIn("UNRELATED_SECRET", os.environ)
            self.assertIsNotNone(application.registry.require(ModelProviderPort))

    async def test_model_transport_settings_are_optional_and_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_MODEL_BASE_URL=http://127.0.0.1:5580\n"
                "TSM_AGT_MODEL=test-model\n"
                "TSM_AGT_MODEL_API_KEY=test-secret\n"
                "TSM_AGT_MODEL_TIMEOUT_SECONDS=240\n"
                "TSM_AGT_MODEL_MAX_RETRIES=4\n"
                "TSM_AGT_MODEL_RETRY_BACKOFF_SECONDS=0.5\n"
                "TSM_AGT_MODEL_OUTPUT_TOKEN_PARAMETER=max_completion_tokens\n"
                "TSM_AGT_MODEL_STRICT_TOOL_SCHEMA=false\n"
                "TSM_AGT_MODEL_STREAMING=false\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_env(
                        env_file
                    )
                )
            provider = application.registry.require(ModelProviderPort)
            self.assertEqual(provider._timeout_seconds, 240.0)
            self.assertEqual(provider._max_retries, 4)
            self.assertEqual(provider._retry_backoff_seconds, 0.5)
            self.assertEqual(
                provider._output_token_parameter, "max_completion_tokens"
            )
            self.assertFalse(provider._strict_tool_schema)
            self.assertFalse(provider._streaming)
            self.assertFalse(provider.capabilities.strict_json_schema)

    async def test_context_compaction_settings_enter_effective_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_MODEL_BASE_URL=http://127.0.0.1:5580\n"
                "TSM_AGT_MODEL=test-model\n"
                "TSM_AGT_MODEL_API_KEY=test-secret\n"
                "TSM_AGT_CONTEXT_COMPACTION_RATIO=0.75\n"
                "TSM_AGT_CONTEXT_LATENCY_SOFT_TOKENS=60000\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_env(
                        env_file
                    )
                )
            manager = application.kernel.dependencies.context_manager
            self.assertEqual(manager.trigger_ratio, 0.75)
            self.assertEqual(manager.latency_soft_input_tokens, 60_000)
            self.assertEqual(
                application.kernel.dependencies.configuration_metadata["sources"][
                    "context.compaction_ratio"
                ],
                "env_file",
            )

    async def test_exported_environment_overrides_local_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_MODEL_BASE_URL=http://file.example\n"
                "TSM_AGT_MODEL=file-model\n"
                "TSM_AGT_MODEL_API_KEY=file-secret\n",
                encoding="utf-8",
            )
            exported = {
                "TSM_AGT_MODEL_BASE_URL": "http://exported.example",
                "TSM_AGT_MODEL": "exported-model",
                "TSM_AGT_MODEL_API_KEY": "exported-secret",
            }
            with patch.dict(os.environ, exported, clear=True):
                compose_openai_compatible_readonly_application_from_env(env_file)
                self.assertEqual(
                    os.environ["TSM_AGT_MODEL_BASE_URL"], "http://exported.example"
                )
                self.assertEqual(os.environ["TSM_AGT_MODEL"], "exported-model")

    async def test_engineering_composition_applies_project_budget_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "TSM_AGT_MODEL_BASE_URL=http://127.0.0.1:5580\n"
                "TSM_AGT_MODEL=test-model\n"
                "TSM_AGT_MODEL_API_KEY=test-secret\n"
                "TSM_AGT_AGENT_MAX_MODEL_CALLS=18\n"
                "TSM_AGT_AGENT_MAX_TOOL_CALLS=48\n"
                "TSM_AGT_AGENT_FINALIZATION_MODEL_CALLS=3\n"
                "TSM_AGT_EXPLORATION_MAX_TOOL_CALLS=32\n"
                "TSM_AGT_EXPLORATION_MAX_ACTIONS=31\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_env(
                        env_file
                    )
                )
            policy = application.registry.require(ExplorationBudgetPolicyPort)
            self.assertEqual(policy.max_total_tool_calls, 32)
            self.assertEqual(policy.max_scored_actions, 31)
            self.assertEqual(application.kernel.dependencies.default_max_model_calls, 18)
            self.assertEqual(application.kernel.dependencies.default_max_tool_calls, 48)
            self.assertEqual(application.kernel.dependencies.finalization_model_calls, 3)
            metadata = application.kernel.dependencies.configuration_metadata
            self.assertEqual(metadata["exploration_budget"]["max_tool_calls"], 32)
            self.assertEqual(
                metadata["exploration_budget"]["finalization_model_calls"], 3
            )
            self.assertEqual(
                metadata["sources"]["exploration_budget.max_tool_calls"],
                "env_file",
            )


if __name__ == "__main__":
    unittest.main()
