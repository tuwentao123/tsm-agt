from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.anthropic_messages import AnthropicMessagesModelProvider
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.adapters.builtin.skill_tools import CoreSkillToolProvider
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.bootstrap import (
    OrchestrationConfiguration,
    compose_local_flow_query_application,
    compose_openai_compatible_engineering_application,
    compose_fixture_application,
    compose_openai_compatible_engineering_application_from_config,
    compose_openai_compatible_readonly_application_from_config,
    compose_readonly_application,
)
from tsm_agt.bootstrap.exploration_configuration import (
    ExplorationBudgetConfiguration,
)
from tsm_agt.bootstrap.orchestration_configuration import (
    load_orchestration_configuration,
)
from tsm_agt.core import (
    ApprovalDecision, ApprovalRequired, ProjectTrustLevel, TaskState,
)
from tsm_agt.core import OrchestrationMode
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
    CompletionReviewerPort,
    ProcessExecutorPort,
    RuntimeInputClassifierPort,
    RuntimeStorePort,
    SandboxPort,
    SessionInputResolverPort,
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
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patcher = patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=UserSkillsStore(Path(home.name)))
        patcher.start()
        self.addCleanup(patcher.stop)

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
            self.assertEqual(
                application.registry.all(SessionInputResolverPort), ()
            )
            self.assertEqual(
                application.registry.all(RuntimeInputClassifierPort), ()
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
            ):
                health = await application.registry.require(port).health()
                self.assertEqual(health.state, "healthy")
            for provider in application.registry.all(ToolProviderPort):
                health = await provider.health()
                self.assertEqual(health.state, "healthy")
        finally:
            await application.registry.stop_all()

    async def test_tool_providers_are_optional(self) -> None:
        application = compose_fixture_application(tool_adapters=())
        await application.registry.start_all()
        try:
            providers = application.registry.all(ToolProviderPort)
            self.assertEqual(providers, ())
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
                    "core.list_files", "core.search_files",
                    "core.find_files",
                    "core.read_file", "core.search_text", "core.grep_search",
                    "git.status", "git.diff", "git.log", "git.show",
                    "git.blame", "git.branches",
                    "runtime.inspect_task", "runtime.list_task_events",
                    "runtime.inspect_session", "runtime.search_history",
                ],
            )
        finally:
            await application.registry.stop_all()

    async def test_real_provider_composition_requires_private_configuration(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "configuration file"):
                compose_openai_compatible_readonly_application_from_config(
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
            with self.assertRaisesRegex(ValueError, "configuration file"):
                compose_openai_compatible_readonly_application_from_config(
                    Path("/definitely/missing/tsm-agt.env")
                )

    async def test_github_source_tools_are_registered_with_the_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: https://models.example.test/v1
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: test-model
github_source:
  enabled: true
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = compose_openai_compatible_engineering_application_from_config(
                    configuration_file
                )
        await application.registry.start_all()
        self.addAsyncCleanup(application.registry.stop_all)

        names = [tool.name for tool in await application.kernel.list_tools()]
        # ``source.github.resolve`` only routes into the GitHub source adapter's
        # tools, so advertising it without them hands the model targets it
        # cannot call. Both must appear together.
        self.assertIn("source.github.resolve", names)
        self.assertIn("source.github.get_file", names)
        self.assertIn("source.github.list_tree", names)

    async def test_real_provider_engineering_composition_has_process_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            # Hermetic by construction: without an explicit configuration file this test
            # would read the developer's real private configuration, so an unrelated local
            # setting could decide (or reject) the composition under test.
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: https://models.example.test/v1
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: test-model
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = compose_openai_compatible_engineering_application_from_config(
                    configuration_file
                )
        await application.registry.start_all()
        try:
            self.assertEqual(
                application.kernel.dependencies.default_max_output_tokens, 8192
            )
            self.assertEqual(
                len(application.registry.all(SessionInputResolverPort)), 1
            )
            self.assertEqual(
                len(application.registry.all(RuntimeInputClassifierPort)), 1
            )
            model = application.registry.require(ModelProviderPort)
            self.assertTrue(model.capabilities.tools)
            self.assertIsNotNone(
                application.registry.require(CompletionReviewerPort)
            )
            sandbox = application.registry.require(SandboxPort)
            self.assertEqual(
                sandbox.descriptor.adapter_id,
                "builtin.local-workspace-command-gate",
            )
            self.assertNotIn("deny-all", sandbox.descriptor.capabilities)
            self.assertEqual(
                [tool.name for tool in await application.kernel.list_tools()],
                [
                    "core.list_files", "core.search_files",
                    "core.find_files",
                    "core.read_file", "core.search_text", "core.grep_search",
                    "git.status", "git.diff", "git.log", "git.show",
                    "git.blame", "git.branches",
                    "runtime.inspect_task", "runtime.list_task_events",
                    "runtime.inspect_session", "runtime.search_history",
                    "web.search", "web.fetch_markdown", "web.open",
                    "web.find_in_page", "web.read", "content.summarize",
                    "core.run_command", "core.process_list", "core.process_wait",
                    "core.process_status", "core.process_logs",
                    "core.process_stop", "core.apply_patch",
                    "core.apply_patch_v4a", "core.apply_patches", "core.delete_file",
                    "core.rollback_mutation",
                    "core.rollback_mutations",
                    "core.rollback_mutation_batch",
                    "core.rollback_mutation_groups",
                    "core.memory_list", "core.memory_remember",
                    "core.memory_verify", "core.memory_forget",
                    "core.working_memory_read",
                    "core.working_memory_update", "core.todo_list",
                    "core.task_spec_read",
                    "core.task_spec_update",
                    "code.symbol_overview", "code.definition",
                    "code.references", "code.implementations",
                    "code.workspace_symbols", "code.diagnostics",
                    "code.rename_preview", "core.request_input", "skill.load",
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
                    {"argv": [sys.executable, script.name], "cwd": "."},
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
                self.assertEqual(result.data["mode"], "foreground")
                self.assertEqual(result.data["stdout"]["text"], "gate-ok\n")
            finally:
                await application.registry.stop_all()

    async def test_private_yaml_does_not_mutate_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: http://127.0.0.1:5580
      auth:
        api_key: local-secret
  profiles:
    primary:
      provider: local
      model: local-model
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = compose_openai_compatible_readonly_application_from_config(
                    configuration_file
                )
                self.assertNotIn("TSM_AGT_OPEN_MODEL", os.environ)
                self.assertNotIn("UNRELATED_SECRET", os.environ)
            self.assertIsNotNone(application.registry.require(ModelProviderPort))

    async def test_model_transport_settings_are_optional_and_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: http://127.0.0.1:5580
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: test-model
      options:
        timeout_seconds: 240
        max_retries: 4
        retry_backoff_seconds: 0.5
        output_token_parameter: max_tokens
        strict_tool_schema: false
        streaming: false
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_config(
                        configuration_file
                    )
                )
            provider = application.registry.require(ModelProviderPort)
            self.assertEqual(provider._timeout_seconds, 240.0)
            self.assertEqual(provider._max_retries, 4)
            self.assertEqual(provider._retry_backoff_seconds, 0.5)
            self.assertEqual(
                provider._output_token_parameter, "max_tokens"
            )
            metadata = application.kernel.dependencies.configuration_metadata
            self.assertEqual(metadata["model"]["protocol"], "open")
            self.assertEqual(metadata["model"]["output_token_parameter"], "max_tokens")
            self.assertFalse(provider._strict_tool_schema)
            self.assertFalse(provider._streaming)
            self.assertFalse(provider.capabilities.strict_json_schema)

    async def test_native_anthropic_protocol_selects_messages_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: anthropic_messages
      base_url: https://gateway.example.test
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: claude-gateway-model
      options:
        anthropic_version: '2023-06-01'
        streaming: false
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = compose_openai_compatible_engineering_application_from_config(configuration_file)
            provider = application.registry.require(ModelProviderPort)
            self.assertIsInstance(provider._provider, AnthropicMessagesModelProvider)
            self.assertEqual(provider._provider._anthropic_version, "2023-06-01")
            metadata = application.kernel.dependencies.configuration_metadata
            self.assertEqual(metadata["model"]["provider"], "anthropic-messages")
            self.assertEqual(metadata["model"]["protocol"], "anthropic")

    async def test_context_compaction_settings_enter_effective_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: http://127.0.0.1:5580
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: test-model
context:
  compaction_ratio: 0.75
  latency_soft_tokens: 60000
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_config(
                        configuration_file
                    )
                )
            manager = application.kernel.dependencies.context_manager
            self.assertEqual(manager.trigger_ratio, 0.75)
            self.assertEqual(manager.latency_soft_input_tokens, 60_000)
            self.assertEqual(
                application.kernel.dependencies.configuration_metadata["sources"][
                    "context.compaction_ratio"
                ],
                "yaml",
            )

    async def test_session_history_bounds_enter_configuration_and_the_projector(
        self,
    ) -> None:
        """The multi-turn fidelity bound is reachable from configuration.

        Before this, ``SessionContextProjector``'s 8-message / 4-summary bounds
        were frozen dataclass defaults with no construction site outside the
        Kernel default factory, so the documented remedy for a too-small bound
        ("raise it, do not re-architect") was not actually available.
        """

        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: http://127.0.0.1:5580
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: test-model
session:
  recent_messages: 20
  task_summaries: 9
context:
  recent_message_floor: 20
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_config(
                        configuration_file
                    )
                )
            projector = application.kernel.dependencies.session_context_projector
            self.assertEqual(projector.recent_visible_message_limit, 20)
            self.assertEqual(projector.detailed_task_summary_limit, 9)
            self.assertEqual(
                application.kernel.dependencies.context_manager.recent_message_floor,
                20,
            )
            self.assertEqual(
                application.kernel.dependencies.configuration_metadata["sources"][
                    "session.recent_messages"
                ],
                "yaml",
            )

    async def test_absent_session_bounds_keep_the_historic_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: http://127.0.0.1:5580
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: test-model
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_config(
                        configuration_file
                    )
                )
            projector = application.kernel.dependencies.session_context_projector
            self.assertEqual(projector.recent_visible_message_limit, 8)
            self.assertEqual(projector.detailed_task_summary_limit, 4)

    def test_non_positive_session_bounds_are_rejected(self) -> None:
        from tsm_agt.bootstrap.context_configuration import ContextConfiguration

        with self.assertRaises(ValueError):
            ContextConfiguration(session_recent_messages=0)
        with self.assertRaises(ValueError):
            ContextConfiguration(session_task_summaries=0)
        with self.assertRaises(ValueError):
            ContextConfiguration(recent_message_floor=0)

    async def test_environment_cannot_override_private_yaml(self) -> None:
        from tests.test_model_profiles import _configuration
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(_configuration(), encoding="utf-8")
            exported = {"TSM_AGT_MODEL_BASE_URL": "http://exported.example", "TSM_AGT_MODEL": "exported-model", "TSM_AGT_MODEL_API_KEY": "exported-secret"}
            with patch.dict(os.environ, exported, clear=True):
                application = compose_openai_compatible_readonly_application_from_config(path)
                provider = application.registry.require(ModelProviderPort)._provider
                self.assertEqual(provider._model, "local-model")
                self.assertEqual(provider._base_url, "http://127.0.0.1:8000/v1")
                self.assertEqual(os.environ["TSM_AGT_MODEL"], "exported-model")

    async def test_engineering_composition_applies_project_budget_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: http://127.0.0.1:5580
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: test-model
agent:
  max_model_calls: 18
  max_tool_calls: 48
  finalization_model_calls: 3
exploration:
  max_tool_calls: 32
  max_actions: 31
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_config(
                        configuration_file
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
                "yaml",
            )
    def test_fixture_defaults_to_single_loop_orchestration(self) -> None:
        application = compose_fixture_application()

        self.assertEqual(
            application.kernel.dependencies.orchestration_mode,
            OrchestrationMode.CONVERSATION_LOOP,
        )

    def test_fixture_can_select_legacy_orchestration(self) -> None:
        with self.assertRaisesRegex(TypeError, "orchestration_mode"):
            compose_fixture_application(orchestration_mode="legacy")

    def test_fixture_can_select_single_loop_orchestration(self) -> None:
        application = compose_fixture_application()
        self.assertEqual(application.kernel.dependencies.configuration_metadata["orchestration"], {"mode": "conversation_loop"})

    def test_orchestration_configuration_rejects_fallback(self) -> None:
        with self.assertRaisesRegex(TypeError, "single_loop_allow_fallback"):
            OrchestrationConfiguration(single_loop_allow_fallback=True)

    async def test_yaml_orchestration_configuration_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: https://models.example.test/v1
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: test-model
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = compose_openai_compatible_engineering_application_from_config(
                    configuration_file
                )

        self.assertEqual(
            application.kernel.dependencies.orchestration_mode,
            OrchestrationMode.CONVERSATION_LOOP,
        )
        self.assertEqual(
            application.kernel.dependencies.configuration_metadata[
                "sources"
            ]["orchestration.mode"],
            "fixed",
        )
        self.assertNotIn("orchestration.single_loop_allow_fallback", application.kernel.dependencies.configuration_metadata["sources"])


if __name__ == "__main__":
    unittest.main()


class OrchestrationEnvironmentOverrideTest(unittest.TestCase):
    def test_legacy_mode_cannot_override_the_fixed_conversation_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = Path(directory) / "config.yaml"
            configuration_file.write_text(
                '''
version: 1
model:
  default_profile: primary
  providers:
    local:
      protocol: openai_chat
      base_url: https://models.example.test/v1
      auth:
        api_key: test-secret
  profiles:
    primary:
      provider: local
      model: test-model
''',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = compose_openai_compatible_engineering_application_from_config(
                    configuration_file
                )

        self.assertEqual(
            application.kernel.dependencies.orchestration_mode,
            OrchestrationMode.CONVERSATION_LOOP,
        )
        self.assertEqual(
            application.kernel.dependencies.configuration_metadata["sources"][
                "orchestration.mode"
            ],
            "fixed",
        )


class ConversationLoopConfigurationTest(unittest.TestCase):
    """The feature flag that selects the model-led loop for new Tasks."""

    def _configuration_file(self, directory: str, extra: str = "") -> Path:
        from tests.test_model_profiles import _configuration
        path = Path(directory) / "config.yaml"
        path.write_text(_configuration() + extra, encoding="utf-8")
        return path

    def test_default_selects_the_conversation_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = self._configuration_file(directory)
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_config(
                        configuration_file
                    )
                )

        self.assertEqual(
            application.kernel.dependencies.orchestration_mode,
            OrchestrationMode.CONVERSATION_LOOP,
        )
        sources = application.kernel.dependencies.configuration_metadata["sources"]
        self.assertEqual(sources["orchestration.mode"], "fixed")
        self.assertNotIn("orchestration.conversation_loop_enabled", sources)

    def test_deprecated_flag_cannot_disable_conversation_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = self._configuration_file(
                directory, "orchestration: {conversation_loop_enabled: false}\n"
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_config(
                        configuration_file
                    )
                )

        self.assertEqual(
            application.kernel.dependencies.orchestration_mode,
            OrchestrationMode.CONVERSATION_LOOP,
        )
        sources = application.kernel.dependencies.configuration_metadata["sources"]
        self.assertEqual(sources["orchestration.mode"], "fixed")
        self.assertNotIn("orchestration.conversation_loop_enabled", sources)

    def test_explicit_mode_wins_over_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration_file = self._configuration_file(
                directory,
                "orchestration: {mode: single_loop, conversation_loop_enabled: true}\n",
            )
            with patch.dict(os.environ, {}, clear=True):
                application = (
                    compose_openai_compatible_engineering_application_from_config(
                        configuration_file
                    )
                )

        self.assertEqual(
            application.kernel.dependencies.orchestration_mode,
            OrchestrationMode.CONVERSATION_LOOP,
        )
        sources = application.kernel.dependencies.configuration_metadata["sources"]
        self.assertEqual(sources["orchestration.mode"], "fixed")

    def test_obsolete_flags_do_not_change_the_fixed_mode(self) -> None:
        configuration = load_orchestration_configuration({"orchestration": {"conversation_loop_enabled": "maybe"}})
        self.assertIs(configuration.mode, OrchestrationMode.CONVERSATION_LOOP)
        self.assertEqual(configuration.sources, {"orchestration.mode": "fixed"})

    def test_conversation_loop_mode_value_is_accepted(self) -> None:
        configuration = load_orchestration_configuration({"orchestration": {"mode": "conversation_loop"}})
        self.assertEqual(configuration.mode, OrchestrationMode.CONVERSATION_LOOP)
