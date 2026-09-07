"""The only composition root allowed to instantiate concrete adapters."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from collections.abc import Mapping

from tsm_agt.adapters import AdapterRegistry
from tsm_agt.adapters.builtin import (
    CoreProcessToolProvider, CoreReadOnlyToolProvider,
    CoreWorkspaceMutationToolProvider, CoreMemoryToolProvider,
    CodeIntelligenceToolProvider, CoreInteractionToolProvider,
    CoreWorkingMemoryToolProvider, CoreTaskSpecToolProvider,
)
from tsm_agt.adapters.text_code_intelligence import TextCodeIntelligenceProvider
from tsm_agt.adapters.structured_evidence import StructuredEvidenceDeltaEvaluator
from tsm_agt.adapters.rule_based_semantic_action import (
    RuleBasedSemanticActionClassifier,
)
from tsm_agt.adapters.rule_based_read_hits import RuleBasedReadHitsPolicy
from tsm_agt.adapters.rule_based_artifact_read import RuleBasedArtifactReadPolicy
from tsm_agt.adapters.rule_based_progressive_scope import (
    RuleBasedProgressiveScopePolicy,
)
from tsm_agt.adapters.rule_based_exploration_budget import (
    RuleBasedExplorationBudgetPolicy,
)
from tsm_agt.adapters.rule_based_stop_or_pivot import RuleBasedStopOrPivotPolicy
from tsm_agt.adapters.evidence_guided import (
    BuiltinEvidenceRelationProvider, BoundedRejectionLoopPolicy,
    RuleBasedEvidenceRelationPolicy, RuleBasedExplorationOutcomePolicy,
)
from tsm_agt.adapters.rule_based_agent_progress import RuleBasedAgentProgressProjector
from tsm_agt.adapters.adaptive_tool_presentation import (
    AdaptiveToolArgumentPresenter,
)
from tsm_agt.adapters.rule_based_evidence_level import (
    RuleBasedEvidenceLevelEvaluator,
)
from tsm_agt.adapters.rule_based_investigation_status import (
    RuleBasedInvestigationStatusProjector,
)
from tsm_agt.adapters.rule_based_investigation_flow import (
    RuleBasedInvestigationFlowProjector,
)
from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.adapters.local_process import LocalProcessExecutor
from tsm_agt.adapters.local_flow_export import (
    LocalFlowArtifactExporter, LocalReplayCursorStore,
)
from tsm_agt.adapters.windows_process import WindowsProcessExecutor
from tsm_agt.adapters.local_sandbox import LocalWorkspaceSandbox
from tsm_agt.adapters.posix_lock import PosixCrossProcessLock
from tsm_agt.adapters.windows_lock import WindowsCrossProcessLock
from tsm_agt.adapters.posix_filesystem import PosixWorkspaceFilesystem
from tsm_agt.adapters.windows_filesystem import WindowsWorkspaceFilesystem
from tsm_agt.adapters.posix_path import PosixWorkspacePath
from tsm_agt.adapters.windows_path import WindowsWorkspacePath
from tsm_agt.adapters.posix_identity import PosixLocalIdentity
from tsm_agt.adapters.windows_identity import WindowsLocalIdentity
from tsm_agt.adapters.sqlite import (
    SQLiteProjectMemoryStore, SQLiteRuntimeReader, SQLiteRuntimeStore,
)
from tsm_agt.adapters.fixture import (
    DenyAllSandbox,
    EchoModelProvider,
    EchoToolProvider,
    InMemoryRuntimeStore,
    InMemoryProjectMemoryStore,
    ToolCallingModelProvider,
)
from tsm_agt.core import ContextWindowManager, Kernel, KernelDependencies
from tsm_agt.ports import (
    CrossProcessLockPort,
    FlowArtifactExportPort,
    ReplayCursorStorePort,
    ModelProviderPort,
    ProcessExecutorPort,
    RuntimeStorePort,
    SandboxPort,
    ToolProviderPort,
    WorkspaceFilesystemPort,
    WorkspacePathPort,
    LocalIdentityPort,
    ProjectMemoryPort,
    CodeIntelligencePort,
    RuntimeInputClassifierPort,
    EvidenceDeltaEvaluatorPort,
    SemanticActionClassifierPort,
    ReadHitsPolicyPort,
    ArtifactReadPolicyPort,
    ProgressiveScopePolicyPort,
    ExplorationBudgetPolicyPort,
    StopOrPivotPolicyPort,
    AgentProgressProjectorPort,
    ToolArgumentPresenterPort,
    EvidenceLevelEvaluatorPort,
    InvestigationStatusProjectorPort,
    InvestigationFlowProjectorPort,
    EvidenceRelationProviderPort, EvidenceRelationPolicyPort,
    RejectionLoopPolicyPort, ExplorationOutcomePolicyPort,
)
from .model_configuration import (
    endpoint_origin, load_model_configuration, model_configuration_sources,
)
from .exploration_configuration import (
    ExplorationBudgetConfiguration, load_exploration_budget_configuration,
)
from .context_configuration import (
    ContextConfiguration, load_context_configuration,
)


@dataclass(frozen=True, slots=True)
class Application:
    kernel: Kernel
    registry: AdapterRegistry


def _endpoint_origin(base_url: str) -> str:
    return endpoint_origin(base_url)


def _configuration_metadata(
    *, provider: str, model: str, base_url: str | None = None,
    credentials_configured: bool = False,
    sources: Mapping[str, str] | None = None,
    exploration_budget: ExplorationBudgetConfiguration | None = None,
    context_configuration: ContextConfiguration | None = None,
    output_token_parameter: str = "max_tokens",
    strict_tool_schema: bool = True,
) -> dict[str, object]:
    data: dict[str, object] = {
        "provider": provider,
        "model": model,
        "credentials_configured": credentials_configured,
        "output_token_parameter": output_token_parameter,
        "strict_tool_schema": strict_tool_schema,
    }
    if base_url is not None:
        data["endpoint_origin"] = _endpoint_origin(base_url)
    result: dict[str, object] = {
        "model": data,
        "sources": dict(sources or {}),
    }
    if exploration_budget is not None:
        result["exploration_budget"] = exploration_budget.snapshot_data()
        result["sources"].update(exploration_budget.sources or {})
    if context_configuration is not None:
        result["sources"].update(context_configuration.sources or {})
    return result


def _kernel_dependencies(
    registry: AdapterRegistry, configuration_metadata: Mapping[str, object],
    context_manager: ContextWindowManager | None = None,
    *, require_evidence_questions: bool = True,
) -> KernelDependencies:
    classifiers = registry.all(RuntimeInputClassifierPort)
    if len(classifiers) > 1:
        raise ValueError("at most one RuntimeInputClassifierPort may be registered")
    evidence_evaluators = registry.all(EvidenceDeltaEvaluatorPort)
    if len(evidence_evaluators) > 1:
        raise ValueError(
            "at most one EvidenceDeltaEvaluatorPort may be registered"
        )
    semantic_classifiers = registry.all(SemanticActionClassifierPort)
    if len(semantic_classifiers) > 1:
        raise ValueError(
            "at most one SemanticActionClassifierPort may be registered"
        )
    read_hits_policies = registry.all(ReadHitsPolicyPort)
    if len(read_hits_policies) > 1:
        raise ValueError("at most one ReadHitsPolicyPort may be registered")
    artifact_read_policies = registry.all(ArtifactReadPolicyPort)
    if len(artifact_read_policies) > 1:
        raise ValueError("at most one ArtifactReadPolicyPort may be registered")
    scope_policies = registry.all(ProgressiveScopePolicyPort)
    if len(scope_policies) > 1:
        raise ValueError("at most one ProgressiveScopePolicyPort may be registered")
    budget_policies = registry.all(ExplorationBudgetPolicyPort)
    if len(budget_policies) > 1:
        raise ValueError("at most one ExplorationBudgetPolicyPort may be registered")
    pivot_policies = registry.all(StopOrPivotPolicyPort)
    if len(pivot_policies) > 1:
        raise ValueError("at most one StopOrPivotPolicyPort may be registered")
    relation_providers = registry.all(EvidenceRelationProviderPort)
    relation_policies = registry.all(EvidenceRelationPolicyPort)
    if len(relation_policies) > 1:
        raise ValueError("at most one EvidenceRelationPolicyPort may be registered")
    rejection_policies = registry.all(RejectionLoopPolicyPort)
    if len(rejection_policies) > 1:
        raise ValueError("at most one RejectionLoopPolicyPort may be registered")
    outcome_policies = registry.all(ExplorationOutcomePolicyPort)
    if len(outcome_policies) > 1:
        raise ValueError("at most one ExplorationOutcomePolicyPort may be registered")
    exploration_policy_counts = (
        len(relation_policies), len(rejection_policies), len(outcome_policies)
    )
    if any(exploration_policy_counts) and not all(exploration_policy_counts):
        raise ValueError(
            "evidence-guided exploration requires relation, rejection, and outcome policies"
        )
    progress_projectors = registry.all(AgentProgressProjectorPort)
    argument_presenters = registry.all(ToolArgumentPresenterPort)
    if len(argument_presenters) > 1:
        raise ValueError("at most one ToolArgumentPresenterPort may be registered")
    if len(progress_projectors) > 1:
        raise ValueError("at most one AgentProgressProjectorPort may be registered")
    evidence_level_evaluators = registry.all(EvidenceLevelEvaluatorPort)
    if len(evidence_level_evaluators) > 1:
        raise ValueError("at most one EvidenceLevelEvaluatorPort may be registered")
    status_projectors = registry.all(InvestigationStatusProjectorPort)
    if len(status_projectors) > 1:
        raise ValueError(
            "at most one InvestigationStatusProjectorPort may be registered"
        )
    investigation_flow_projectors = registry.all(InvestigationFlowProjectorPort)
    if len(investigation_flow_projectors) > 1:
        raise ValueError(
            "at most one InvestigationFlowProjectorPort may be registered"
        )
    raw_budget = configuration_metadata.get("exploration_budget", {})
    budget = dict(raw_budget) if isinstance(raw_budget, Mapping) else {}
    return KernelDependencies(
        model=registry.require(ModelProviderPort),
        store=registry.require(RuntimeStorePort),
        sandbox=registry.require(SandboxPort),
        cross_process_lock=registry.require(CrossProcessLockPort),
        workspace_filesystem=registry.require(WorkspaceFilesystemPort),
        workspace_path=registry.require(WorkspacePathPort),
        local_identity=registry.require(LocalIdentityPort),
        project_memory=registry.require(ProjectMemoryPort),
        evidence_delta_evaluator=(
            evidence_evaluators[0] if evidence_evaluators else None
        ),
        semantic_action_classifier=(
            semantic_classifiers[0] if semantic_classifiers else None
        ),
        read_hits_policy=(
            read_hits_policies[0] if read_hits_policies else None
        ),
        artifact_read_policy=(
            artifact_read_policies[0] if artifact_read_policies else None
        ),
        progressive_scope_policy=(scope_policies[0] if scope_policies else None),
        exploration_budget_policy=(
            budget_policies[0] if budget_policies else None
        ),
        stop_or_pivot_policy=(pivot_policies[0] if pivot_policies else None),
        evidence_relation_providers=relation_providers,
        evidence_relation_policy=(
            relation_policies[0] if relation_policies else None
        ),
        rejection_loop_policy=(
            rejection_policies[0] if rejection_policies else None
        ),
        exploration_outcome_policy=(
            outcome_policies[0] if outcome_policies else None
        ),
        agent_progress_projector=(
            progress_projectors[0] if progress_projectors else None
        ),
        tool_argument_presenter=(
            argument_presenters[0] if argument_presenters else None
        ),
        evidence_level_evaluator=(
            evidence_level_evaluators[0] if evidence_level_evaluators else None
        ),
        investigation_status_projector=(
            status_projectors[0] if status_projectors else None
        ),
        investigation_flow_projector=(
            investigation_flow_projectors[0]
            if investigation_flow_projectors else None
        ),
        runtime_input_classifier=(classifiers[0] if classifiers else None),
        process_executor=registry.require(ProcessExecutorPort),
        tools=registry.all(ToolProviderPort),
        runtime_adapters=registry.runtime_adapters(),
        configuration_metadata=configuration_metadata,
        default_max_model_calls=int(budget.get("agent_max_model_calls", 15)),
        default_max_tool_calls=int(budget.get("agent_max_tool_calls", 40)),
        finalization_model_calls=int(budget.get("finalization_model_calls", 2)),
        context_manager=context_manager or ContextWindowManager(),
        require_evidence_questions=require_evidence_questions,
    )


def _platform_cross_process_lock() -> CrossProcessLockPort:
    if os.name == "nt":
        return WindowsCrossProcessLock()
    return PosixCrossProcessLock()


def _platform_process_executor() -> ProcessExecutorPort:
    if os.name == "nt":
        return WindowsProcessExecutor()
    return LocalProcessExecutor()


def _platform_workspace_filesystem() -> WorkspaceFilesystemPort:
    if os.name == "nt":
        return WindowsWorkspaceFilesystem()
    return PosixWorkspaceFilesystem()


def _platform_workspace_path() -> WorkspacePathPort:
    if os.name == "nt":
        return WindowsWorkspacePath()
    return PosixWorkspacePath()


def _platform_local_identity() -> LocalIdentityPort:
    if os.name == "nt":
        return WindowsLocalIdentity()
    return PosixLocalIdentity()


def _register_exploration_profile(
    registry: AdapterRegistry, profile: str | None = None,
) -> str:
    """Register one complete replaceable exploration strategy profile."""

    selected = (profile or "balanced").strip().casefold()
    if selected == "legacy":
        registry.register(
            ProgressiveScopePolicyPort, RuleBasedProgressiveScopePolicy()
        )
        return selected
    if selected != "balanced":
        raise ValueError(
            "TSM_AGT_EXPLORATION_PROFILE must be 'balanced' or 'legacy'"
        )
    registry.register(
        EvidenceRelationProviderPort, BuiltinEvidenceRelationProvider()
    )
    registry.register(
        EvidenceRelationPolicyPort, RuleBasedEvidenceRelationPolicy()
    )
    registry.register(RejectionLoopPolicyPort, BoundedRejectionLoopPolicy())
    registry.register(
        ExplorationOutcomePolicyPort, RuleBasedExplorationOutcomePolicy()
    )
    return selected


def compose_fixture_application(
    model_adapter: ModelProviderPort | None = None,
    tool_adapters: tuple[ToolProviderPort, ...] | None = None,
    store_adapter: RuntimeStorePort | None = None,
    process_adapter: ProcessExecutorPort | None = None,
    sandbox_adapter: SandboxPort | None = None,
    cross_process_lock_adapter: CrossProcessLockPort | None = None,
    workspace_filesystem_adapter: WorkspaceFilesystemPort | None = None,
    workspace_path_adapter: WorkspacePathPort | None = None,
    local_identity_adapter: LocalIdentityPort | None = None,
    project_memory_adapter: ProjectMemoryPort | None = None,
    context_manager: ContextWindowManager | None = None,
    enable_working_memory: bool = False,
    runtime_input_classifier_adapter: RuntimeInputClassifierPort | None = None,
    require_evidence_questions: bool = False,
    evidence_delta_evaluator_adapter: EvidenceDeltaEvaluatorPort | None = None,
    semantic_action_classifier_adapter: SemanticActionClassifierPort | None = None,
    read_hits_policy_adapter: ReadHitsPolicyPort | None = None,
    artifact_read_policy_adapter: ArtifactReadPolicyPort | None = None,
    progressive_scope_policy_adapter: ProgressiveScopePolicyPort | None = None,
    exploration_budget_policy_adapter: ExplorationBudgetPolicyPort | None = None,
    stop_or_pivot_policy_adapter: StopOrPivotPolicyPort | None = None,
    evidence_relation_provider_adapters: (
        tuple[EvidenceRelationProviderPort, ...] | None
    ) = None,
    evidence_relation_policy_adapter: EvidenceRelationPolicyPort | None = None,
    rejection_loop_policy_adapter: RejectionLoopPolicyPort | None = None,
    exploration_outcome_policy_adapter: ExplorationOutcomePolicyPort | None = None,
    agent_progress_projector_adapter: AgentProgressProjectorPort | None = None,
    tool_argument_presenter_adapter: ToolArgumentPresenterPort | None = None,
    evidence_level_evaluator_adapter: EvidenceLevelEvaluatorPort | None = None,
    investigation_status_projector_adapter: (
        InvestigationStatusProjectorPort | None
    ) = None,
    investigation_flow_projector_adapter: (
        InvestigationFlowProjectorPort | None
    ) = None,
) -> Application:
    registry = AdapterRegistry()
    registry.register(ModelProviderPort, model_adapter or EchoModelProvider())
    registry.register(RuntimeStorePort, store_adapter or InMemoryRuntimeStore())
    registry.register(SandboxPort, sandbox_adapter or DenyAllSandbox())
    registry.register(
        ProcessExecutorPort, process_adapter or _platform_process_executor()
    )
    registry.register(
        CrossProcessLockPort,
        cross_process_lock_adapter or _platform_cross_process_lock(),
    )
    registry.register(
        WorkspaceFilesystemPort,
        workspace_filesystem_adapter or _platform_workspace_filesystem(),
    )
    registry.register(
        WorkspacePathPort, workspace_path_adapter or _platform_workspace_path()
    )
    registry.register(
        LocalIdentityPort, local_identity_adapter or _platform_local_identity()
    )
    registry.register(
        ProjectMemoryPort, project_memory_adapter or InMemoryProjectMemoryStore()
    )
    if runtime_input_classifier_adapter is not None:
        registry.register(
            RuntimeInputClassifierPort, runtime_input_classifier_adapter
        )
    if evidence_delta_evaluator_adapter is not None:
        registry.register(
            EvidenceDeltaEvaluatorPort, evidence_delta_evaluator_adapter
        )
    if semantic_action_classifier_adapter is not None:
        registry.register(
            SemanticActionClassifierPort, semantic_action_classifier_adapter
        )
    if read_hits_policy_adapter is not None:
        registry.register(ReadHitsPolicyPort, read_hits_policy_adapter)
    if artifact_read_policy_adapter is not None:
        registry.register(ArtifactReadPolicyPort, artifact_read_policy_adapter)
    if progressive_scope_policy_adapter is not None:
        registry.register(
            ProgressiveScopePolicyPort, progressive_scope_policy_adapter
        )
    if exploration_budget_policy_adapter is not None:
        registry.register(
            ExplorationBudgetPolicyPort, exploration_budget_policy_adapter
        )
    if stop_or_pivot_policy_adapter is not None:
        registry.register(StopOrPivotPolicyPort, stop_or_pivot_policy_adapter)
    for provider in evidence_relation_provider_adapters or ():
        registry.register(EvidenceRelationProviderPort, provider)
    if evidence_relation_policy_adapter is not None:
        registry.register(
            EvidenceRelationPolicyPort, evidence_relation_policy_adapter
        )
    if rejection_loop_policy_adapter is not None:
        registry.register(RejectionLoopPolicyPort, rejection_loop_policy_adapter)
    if exploration_outcome_policy_adapter is not None:
        registry.register(
            ExplorationOutcomePolicyPort, exploration_outcome_policy_adapter
        )
    if agent_progress_projector_adapter is not None:
        registry.register(
            AgentProgressProjectorPort, agent_progress_projector_adapter
        )
    registry.register(
        ToolArgumentPresenterPort,
        tool_argument_presenter_adapter or AdaptiveToolArgumentPresenter(),
    )
    if evidence_level_evaluator_adapter is not None:
        registry.register(
            EvidenceLevelEvaluatorPort, evidence_level_evaluator_adapter
        )
    if investigation_status_projector_adapter is not None:
        registry.register(
            InvestigationStatusProjectorPort,
            investigation_status_projector_adapter,
        )
    if investigation_flow_projector_adapter is not None:
        registry.register(
            InvestigationFlowProjectorPort, investigation_flow_projector_adapter
        )
    selected_tools = (EchoToolProvider(),) if tool_adapters is None else tool_adapters
    for tool_adapter in selected_tools:
        registry.register(ToolProviderPort, tool_adapter)

    selected_model = registry.require(ModelProviderPort)
    if selected_model.capabilities.tools and enable_working_memory:
        registry.register(ToolProviderPort, CoreWorkingMemoryToolProvider())
        registry.register(ToolProviderPort, CoreTaskSpecToolProvider())
    if selected_model.capabilities.tools:
        registry.register(ToolProviderPort, CoreInteractionToolProvider())
    dependencies = _kernel_dependencies(
        registry, _configuration_metadata(
            provider="fixture", model=selected_model.descriptor.adapter_id,
            credentials_configured=False,
            sources={
                "model.provider": "harness_default",
                "model.model": "harness_default",
                "model.credentials_configured": "harness_default",
            },
        ), context_manager,
        require_evidence_questions=require_evidence_questions,
    )
    return Application(kernel=Kernel(dependencies), registry=registry)


def compose_fixture_agent_application() -> Application:
    """Compose the deterministic tool-calling model used by Agent loop demos."""

    return compose_fixture_application(model_adapter=ToolCallingModelProvider())


def compose_local_project_control_application(
    workspace: Path,
) -> Application:
    """Compose local persistent project controls without model credentials."""

    root = workspace.expanduser().resolve()
    return compose_fixture_application(
        store_adapter=SQLiteRuntimeStore(root / ".agent" / "runtime.db"),
        project_memory_adapter=SQLiteProjectMemoryStore(
            root / ".agent" / "memory.db"
        ),
        tool_adapters=(),
    )


def compose_local_flow_query_application(workspace: Path) -> Application:
    """Compose a read-only runtime view plus explicit artifact export."""

    root = workspace.expanduser().resolve()
    application = compose_fixture_application(
        store_adapter=SQLiteRuntimeReader(root / ".agent" / "runtime.db"),
        tool_adapters=(),
        investigation_flow_projector_adapter=(
            RuleBasedInvestigationFlowProjector()
        ),
    )
    application.registry.register(
        FlowArtifactExportPort,
        LocalFlowArtifactExporter(
            application.registry.require(WorkspacePathPort),
            application.registry.require(WorkspaceFilesystemPort),
        ),
    )
    application.registry.register(
        ReplayCursorStorePort,
        LocalReplayCursorStore(
            application.registry.require(WorkspacePathPort),
            application.registry.require(WorkspaceFilesystemPort),
            application.registry.require(CrossProcessLockPort),
        ),
    )
    return application


def compose_readonly_application(
    model_adapter: ModelProviderPort | None = None,
) -> Application:
    """Compose the built-in workspace reader without any write/process tools."""

    return compose_fixture_application(
        model_adapter=model_adapter,
        tool_adapters=(CoreReadOnlyToolProvider(),),
    )


def compose_openai_compatible_readonly_application(
    *,
    base_url: str,
    model: str,
    api_key: str,
    database_path: Path | None = None,
    configuration_sources: Mapping[str, str] | None = None,
    exploration_budget_configuration: ExplorationBudgetConfiguration | None = None,
    model_timeout_seconds: float = 180.0,
    model_max_retries: int = 2,
    model_retry_backoff_seconds: float = 1.0,
    model_output_token_parameter: str = "max_tokens",
    model_strict_tool_schema: bool = True,
    context_configuration: ContextConfiguration | None = None,
) -> Application:
    """Compose a real OpenAI-compatible model with built-in read-only tools."""

    registry = AdapterRegistry()
    budget = exploration_budget_configuration or ExplorationBudgetConfiguration()
    context = context_configuration or ContextConfiguration()
    registry.register(
        ModelProviderPort, OpenAICompatibleModelProvider(
            base_url, model, api_key, timeout_seconds=model_timeout_seconds,
            max_retries=model_max_retries,
            retry_backoff_seconds=model_retry_backoff_seconds,
            output_token_parameter=model_output_token_parameter,
            strict_tool_schema=model_strict_tool_schema,
        )
    )
    registry.register(
        RuntimeStorePort,
        SQLiteRuntimeStore(database_path or Path.cwd() / ".agent" / "runtime.db"),
    )
    registry.register(SandboxPort, DenyAllSandbox())
    registry.register(ProcessExecutorPort, _platform_process_executor())
    registry.register(CrossProcessLockPort, _platform_cross_process_lock())
    registry.register(WorkspaceFilesystemPort, _platform_workspace_filesystem())
    registry.register(WorkspacePathPort, _platform_workspace_path())
    registry.register(LocalIdentityPort, _platform_local_identity())
    registry.register(
        EvidenceDeltaEvaluatorPort, StructuredEvidenceDeltaEvaluator()
    )
    registry.register(
        SemanticActionClassifierPort, RuleBasedSemanticActionClassifier()
    )
    registry.register(ReadHitsPolicyPort, RuleBasedReadHitsPolicy())
    registry.register(ArtifactReadPolicyPort, RuleBasedArtifactReadPolicy())
    exploration_profile = _register_exploration_profile(registry, budget.profile)
    registry.register(
        ExplorationBudgetPolicyPort,
        RuleBasedExplorationBudgetPolicy(**budget.policy_arguments()),
    )
    registry.register(StopOrPivotPolicyPort, RuleBasedStopOrPivotPolicy())
    registry.register(
        AgentProgressProjectorPort, RuleBasedAgentProgressProjector()
    )
    registry.register(
        ToolArgumentPresenterPort, AdaptiveToolArgumentPresenter()
    )
    registry.register(
        EvidenceLevelEvaluatorPort, RuleBasedEvidenceLevelEvaluator()
    )
    registry.register(
        InvestigationStatusProjectorPort,
        RuleBasedInvestigationStatusProjector(),
    )
    registry.register(
        InvestigationFlowProjectorPort,
        RuleBasedInvestigationFlowProjector(),
    )
    code_intelligence = TextCodeIntelligenceProvider(
        registry.require(WorkspacePathPort)
    )
    registry.register(CodeIntelligencePort, code_intelligence)
    registry.register(ProjectMemoryPort, SQLiteProjectMemoryStore(
        (database_path or Path.cwd() / ".agent" / "runtime.db").parent
        / "memory.db"
    ))
    registry.register(ToolProviderPort, CoreReadOnlyToolProvider())
    registry.register(ToolProviderPort, CoreMemoryToolProvider())
    registry.register(ToolProviderPort, CoreWorkingMemoryToolProvider())
    registry.register(ToolProviderPort, CoreTaskSpecToolProvider())
    registry.register(
        ToolProviderPort, CodeIntelligenceToolProvider(code_intelligence)
    )
    registry.register(ToolProviderPort, CoreInteractionToolProvider())
    dependencies = _kernel_dependencies(
        registry, _configuration_metadata(
            provider="openai-compatible", model=model, base_url=base_url,
            credentials_configured=bool(api_key),
            sources=configuration_sources or {
                "model.provider": "composition",
                "model.model": "argument",
                "model.endpoint_origin": "argument",
                "model.credentials_configured": "argument",
            },
            exploration_budget=budget,
            context_configuration=context,
            output_token_parameter=model_output_token_parameter,
            strict_tool_schema=model_strict_tool_schema,
        ),
        ContextWindowManager(
            trigger_ratio=context.compaction_ratio,
            latency_soft_input_tokens=context.latency_soft_tokens,
        ),
    )
    return Application(kernel=Kernel(dependencies), registry=registry)


def compose_openai_compatible_engineering_application(
    *, base_url: str, model: str, api_key: str,
    database_path: Path | None = None,
    configuration_sources: Mapping[str, str] | None = None,
    exploration_budget_configuration: ExplorationBudgetConfiguration | None = None,
    model_timeout_seconds: float = 180.0,
    model_max_retries: int = 2,
    model_retry_backoff_seconds: float = 1.0,
    model_output_token_parameter: str = "max_tokens",
    model_strict_tool_schema: bool = True,
    context_configuration: ContextConfiguration | None = None,
) -> Application:
    """Compose the engineering Agent with workspace and process tools."""

    registry = AdapterRegistry()
    budget = exploration_budget_configuration or ExplorationBudgetConfiguration()
    context = context_configuration or ContextConfiguration()
    workspace_path = _platform_workspace_path()
    registry.register(
        ModelProviderPort, OpenAICompatibleModelProvider(
            base_url, model, api_key, timeout_seconds=model_timeout_seconds,
            max_retries=model_max_retries,
            retry_backoff_seconds=model_retry_backoff_seconds,
            output_token_parameter=model_output_token_parameter,
            strict_tool_schema=model_strict_tool_schema,
        )
    )
    registry.register(
        RuntimeStorePort,
        SQLiteRuntimeStore(database_path or Path.cwd() / ".agent" / "runtime.db"),
    )
    registry.register(SandboxPort, LocalWorkspaceSandbox(workspace_path))
    registry.register(ProcessExecutorPort, _platform_process_executor())
    registry.register(CrossProcessLockPort, _platform_cross_process_lock())
    registry.register(WorkspaceFilesystemPort, _platform_workspace_filesystem())
    registry.register(WorkspacePathPort, workspace_path)
    registry.register(LocalIdentityPort, _platform_local_identity())
    registry.register(
        EvidenceDeltaEvaluatorPort, StructuredEvidenceDeltaEvaluator()
    )
    registry.register(
        SemanticActionClassifierPort, RuleBasedSemanticActionClassifier()
    )
    registry.register(ReadHitsPolicyPort, RuleBasedReadHitsPolicy())
    registry.register(ArtifactReadPolicyPort, RuleBasedArtifactReadPolicy())
    exploration_profile = _register_exploration_profile(registry, budget.profile)
    registry.register(
        ExplorationBudgetPolicyPort,
        RuleBasedExplorationBudgetPolicy(**budget.policy_arguments()),
    )
    registry.register(StopOrPivotPolicyPort, RuleBasedStopOrPivotPolicy())
    registry.register(
        AgentProgressProjectorPort, RuleBasedAgentProgressProjector()
    )
    registry.register(
        ToolArgumentPresenterPort, AdaptiveToolArgumentPresenter()
    )
    registry.register(
        EvidenceLevelEvaluatorPort, RuleBasedEvidenceLevelEvaluator()
    )
    registry.register(
        InvestigationStatusProjectorPort,
        RuleBasedInvestigationStatusProjector(),
    )
    registry.register(
        InvestigationFlowProjectorPort,
        RuleBasedInvestigationFlowProjector(),
    )
    code_intelligence = TextCodeIntelligenceProvider(workspace_path)
    registry.register(CodeIntelligencePort, code_intelligence)
    registry.register(ProjectMemoryPort, SQLiteProjectMemoryStore(
        (database_path or Path.cwd() / ".agent" / "runtime.db").parent
        / "memory.db"
    ))
    registry.register(ToolProviderPort, CoreReadOnlyToolProvider())
    registry.register(ToolProviderPort, CoreProcessToolProvider())
    registry.register(ToolProviderPort, CoreWorkspaceMutationToolProvider())
    registry.register(ToolProviderPort, CoreMemoryToolProvider())
    registry.register(ToolProviderPort, CoreWorkingMemoryToolProvider())
    registry.register(ToolProviderPort, CoreTaskSpecToolProvider())
    registry.register(
        ToolProviderPort, CodeIntelligenceToolProvider(code_intelligence)
    )
    registry.register(ToolProviderPort, CoreInteractionToolProvider())
    dependencies = _kernel_dependencies(
        registry, _configuration_metadata(
            provider="openai-compatible", model=model, base_url=base_url,
            credentials_configured=bool(api_key),
            sources=configuration_sources or {
                "model.provider": "composition",
                "model.model": "argument",
                "model.endpoint_origin": "argument",
                "model.credentials_configured": "argument",
            },
            exploration_budget=budget,
            context_configuration=context,
            output_token_parameter=model_output_token_parameter,
            strict_tool_schema=model_strict_tool_schema,
        ),
        ContextWindowManager(
            trigger_ratio=context.compaction_ratio,
            latency_soft_input_tokens=context.latency_soft_tokens,
        ),
    )
    return Application(Kernel(dependencies), registry)


def compose_openai_compatible_readonly_application_from_env(
    env_file: Path | None = None,
    database_path: Path | None = None,
) -> Application:
    """Load optional .env, then read the three explicit TSM_AGT_* variables."""

    configuration = load_model_configuration(
        env_file or Path.cwd() / ".env", os.environ
    )
    assert configuration is not None
    for name, value in (
        ("TSM_AGT_MODEL_BASE_URL", configuration.base_url),
        ("TSM_AGT_MODEL", configuration.model),
        ("TSM_AGT_MODEL_API_KEY", configuration.api_key),
    ):
        os.environ.setdefault(name, value)
    budget = load_exploration_budget_configuration(
        env_file or Path.cwd() / ".env", os.environ
    )
    context = load_context_configuration(
        env_file or Path.cwd() / ".env", os.environ
    )
    return compose_openai_compatible_readonly_application(
        base_url=configuration.base_url,
        model=configuration.model,
        api_key=configuration.api_key,
        database_path=database_path,
        configuration_sources=model_configuration_sources(configuration),
        exploration_budget_configuration=budget,
        model_timeout_seconds=configuration.timeout_seconds,
        model_max_retries=configuration.max_retries,
        model_retry_backoff_seconds=configuration.retry_backoff_seconds,
        model_output_token_parameter=configuration.output_token_parameter,
        model_strict_tool_schema=configuration.strict_tool_schema,
        context_configuration=context,
    )


def compose_openai_compatible_engineering_application_from_env(
    env_file: Path | None = None, database_path: Path | None = None,
) -> Application:
    """Load local model settings and compose the engineering Agent."""

    configuration = load_model_configuration(
        env_file or Path.cwd() / ".env", os.environ
    )
    assert configuration is not None
    for name, value in (
        ("TSM_AGT_MODEL_BASE_URL", configuration.base_url),
        ("TSM_AGT_MODEL", configuration.model),
        ("TSM_AGT_MODEL_API_KEY", configuration.api_key),
    ):
        os.environ.setdefault(name, value)
    budget = load_exploration_budget_configuration(
        env_file or Path.cwd() / ".env", os.environ
    )
    context = load_context_configuration(
        env_file or Path.cwd() / ".env", os.environ
    )
    return compose_openai_compatible_engineering_application(
        base_url=configuration.base_url,
        model=configuration.model,
        api_key=configuration.api_key,
        database_path=database_path,
        configuration_sources=model_configuration_sources(configuration),
        exploration_budget_configuration=budget,
        model_timeout_seconds=configuration.timeout_seconds,
        model_max_retries=configuration.max_retries,
        model_retry_backoff_seconds=configuration.retry_backoff_seconds,
        model_output_token_parameter=configuration.output_token_parameter,
        model_strict_tool_schema=configuration.strict_tool_schema,
        context_configuration=context,
    )
