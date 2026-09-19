"""Task state and legal transition rules owned by the microkernel."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from .approval import ApprovalRequest
from .clarification import ClarificationRequest
from .execution import ToolExecutionRecord
from .process import BackgroundProcessRecord
from .trust import ProjectTrustLevel
from .workspace import MutationRecord, WorkspaceBaseline
from .configuration import EffectiveConfigurationSnapshot
from .onboarding import OnboardingCheckpoint, ProjectOnboardingSnapshot
from .session import standalone_session_id
from .workspace_access import WorkspaceAccessGrant


class TaskState(StrEnum):
    CREATED = "CREATED"
    INTAKE = "INTAKE"
    RESOLVING_PROJECT = "RESOLVING_PROJECT"
    SELECTING_EXTENSIONS = "SELECTING_EXTENSIONS"
    ROUTING = "ROUTING"
    PLANNING = "PLANNING"
    RUNNING_WORKFLOW = "RUNNING_WORKFLOW"
    EXECUTING = "EXECUTING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_USER = "AWAITING_USER"
    INTERRUPTING = "INTERRUPTING"
    INTERRUPTED = "INTERRUPTED"
    RESUMING = "RESUMING"
    CONFLICT = "CONFLICT"
    VERIFYING = "VERIFYING"
    FINALIZING = "FINALIZING"
    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.CANCELLED, self.FAILED}


LEGAL_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset({TaskState.INTAKE, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.INTAKE: frozenset(
        {TaskState.RESOLVING_PROJECT, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.RESOLVING_PROJECT: frozenset(
        {TaskState.SELECTING_EXTENSIONS, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.SELECTING_EXTENSIONS: frozenset(
        {TaskState.ROUTING, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.ROUTING: frozenset(
        {
            TaskState.PLANNING,
            TaskState.RUNNING_WORKFLOW,
            TaskState.EXECUTING,
            TaskState.VERIFYING,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.PLANNING: frozenset(
        {TaskState.RUNNING_WORKFLOW, TaskState.EXECUTING, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.RUNNING_WORKFLOW: frozenset(
        {
            TaskState.AWAITING_APPROVAL,
            TaskState.AWAITING_USER,
            TaskState.INTERRUPTING,
            TaskState.CONFLICT,
            TaskState.VERIFYING,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.EXECUTING: frozenset(
        {
            TaskState.AWAITING_APPROVAL,
            TaskState.AWAITING_USER,
            TaskState.INTERRUPTING,
            TaskState.CONFLICT,
            TaskState.VERIFYING,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.AWAITING_APPROVAL: frozenset(
        {TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW, TaskState.INTERRUPTING, TaskState.CONFLICT, TaskState.CANCELLED}
    ),
    TaskState.AWAITING_USER: frozenset(
        {TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW, TaskState.INTERRUPTING, TaskState.CANCELLED}
    ),
    TaskState.INTERRUPTING: frozenset(
        {TaskState.INTERRUPTED, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.INTERRUPTED: frozenset({
        TaskState.RESUMING, TaskState.CONFLICT,
        TaskState.CANCELLED, TaskState.FAILED,
    }),
    TaskState.RESUMING: frozenset(
        {TaskState.EXECUTING, TaskState.RUNNING_WORKFLOW, TaskState.CONFLICT, TaskState.FAILED}
    ),
    TaskState.CONFLICT: frozenset(
        {TaskState.RESUMING, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.VERIFYING: frozenset(
        {TaskState.EXECUTING, TaskState.FINALIZING, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.FINALIZING: frozenset(
        {TaskState.SUCCEEDED, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.FAILED: frozenset(),
}


class InvalidTaskTransition(ValueError):
    def __init__(self, current: TaskState, target: TaskState) -> None:
        super().__init__(f"illegal task transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


class TaskNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    goal: str
    workspace: str
    state: TaskState
    created_at: datetime
    updated_at: datetime
    pending_approval: ApprovalRequest | None = None
    pending_clarification: ClarificationRequest | None = None
    tool_executions: Mapping[str, ToolExecutionRecord] = field(default_factory=dict)
    background_processes: Mapping[str, BackgroundProcessRecord] = field(default_factory=dict)
    project_trust: ProjectTrustLevel = ProjectTrustLevel.UNTRUSTED
    project_fingerprint: str = ""
    trust_subject: str = ""
    workspace_baseline: WorkspaceBaseline | None = None
    mutation_journal: tuple[MutationRecord, ...] = ()
    effective_configurations: tuple[EffectiveConfigurationSnapshot, ...] = ()
    active_agent_checkpoint: Mapping[str, Any] | None = None
    onboarding_snapshots: tuple[ProjectOnboardingSnapshot, ...] = ()
    active_onboarding_checkpoint: OnboardingCheckpoint | None = None
    session_id: str = ""
    workspace_access_grants: tuple[WorkspaceAccessGrant, ...] = ()

    @classmethod
    def create(
        cls, task_id: str, goal: str, workspace: str, now: datetime | None = None,
        session_id: str | None = None,
    ) -> TaskSnapshot:
        timestamp = now or datetime.now(timezone.utc)
        return cls(
            task_id, goal, workspace, TaskState.CREATED, timestamp, timestamp,
            session_id=session_id or standalone_session_id(task_id),
        )

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> TaskSnapshot:
        return cls(
            task_id=str(data["task_id"]),
            goal=str(data["goal"]),
            workspace=str(data["workspace"]),
            state=TaskState(str(data["state"])),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
            pending_approval=(
                ApprovalRequest.from_data(data["pending_approval"])
                if isinstance(data.get("pending_approval"), Mapping)
                else None
            ),
            pending_clarification=(
                ClarificationRequest.from_data(data["pending_clarification"])
                if isinstance(data.get("pending_clarification"), Mapping)
                else None
            ),
            tool_executions=(
                {
                    str(key): ToolExecutionRecord.from_data(value)
                    for key, value in raw_executions.items()
                    if isinstance(value, Mapping)
                }
                if isinstance((raw_executions := data.get("tool_executions")), Mapping)
                else {}
            ),
            background_processes=(
                {
                    str(key): BackgroundProcessRecord.from_data(value)
                    for key, value in raw_processes.items()
                    if isinstance(value, Mapping)
                }
                if isinstance((raw_processes := data.get("background_processes")), Mapping)
                else {}
            ),
            project_trust=ProjectTrustLevel(
                str(data.get("project_trust", ProjectTrustLevel.UNTRUSTED.value))
            ),
            project_fingerprint=str(data.get("project_fingerprint", "")),
            trust_subject=str(data.get("trust_subject", "")),
            workspace_baseline=(
                WorkspaceBaseline.from_data(data["workspace_baseline"])
                if isinstance(data.get("workspace_baseline"), Mapping)
                else None
            ),
            mutation_journal=tuple(
                MutationRecord.from_data(item)
                for item in raw_mutations
                if isinstance(item, Mapping)
            ) if isinstance((raw_mutations := data.get("mutation_journal")), list) else (),
            effective_configurations=tuple(
                EffectiveConfigurationSnapshot.from_data(item)
                for item in raw_configurations
                if isinstance(item, Mapping)
            ) if isinstance(
                (raw_configurations := data.get("effective_configurations")), list
            ) else (),
            active_agent_checkpoint=(
                dict(data["active_agent_checkpoint"])
                if isinstance(data.get("active_agent_checkpoint"), Mapping)
                else None
            ),
            onboarding_snapshots=tuple(
                ProjectOnboardingSnapshot.from_data(item)
                for item in raw_onboarding
                if isinstance(item, Mapping)
            ) if isinstance(
                (raw_onboarding := data.get("onboarding_snapshots")), list
            ) else (),
            active_onboarding_checkpoint=(
                OnboardingCheckpoint.from_data(data["active_onboarding_checkpoint"])
                if isinstance(data.get("active_onboarding_checkpoint"), Mapping)
                else None
            ),
            session_id=str(data.get("session_id") or standalone_session_id(str(data["task_id"]))),
            workspace_access_grants=tuple(
                WorkspaceAccessGrant.from_data(item)
                for item in raw_access_grants
                if isinstance(item, Mapping)
            ) if isinstance(
                (raw_access_grants := data.get("workspace_access_grants")), list
            ) else (),
        )

    def transition(
        self, target: TaskState, now: datetime | None = None
    ) -> TaskSnapshot:
        if target not in LEGAL_TRANSITIONS[self.state]:
            raise InvalidTaskTransition(self.state, target)
        return TaskSnapshot(
            task_id=self.task_id,
            goal=self.goal,
            workspace=self.workspace,
            state=target,
            created_at=self.created_at,
            updated_at=now or datetime.now(timezone.utc),
            pending_approval=(
                None if target.is_terminal else self.pending_approval
            ),
            pending_clarification=(
                None if target.is_terminal else self.pending_clarification
            ),
            tool_executions=self.tool_executions,
            background_processes=self.background_processes,
            project_trust=self.project_trust,
            project_fingerprint=self.project_fingerprint,
            trust_subject=self.trust_subject,
            workspace_baseline=self.workspace_baseline,
            mutation_journal=self.mutation_journal,
            effective_configurations=self.effective_configurations,
            active_agent_checkpoint=(
                None if target.is_terminal else self.active_agent_checkpoint
            ),
            onboarding_snapshots=self.onboarding_snapshots,
            active_onboarding_checkpoint=(
                None if target.is_terminal else self.active_onboarding_checkpoint
            ),
            session_id=self.session_id,
            workspace_access_grants=self.workspace_access_grants,
        )

    def await_approval(
        self, request: ApprovalRequest, now: datetime | None = None
    ) -> TaskSnapshot:
        if self.pending_approval is not None:
            raise ValueError("task already has a pending approval")
        awaiting = self.transition(TaskState.AWAITING_APPROVAL, now)
        return TaskSnapshot(
            task_id=awaiting.task_id,
            goal=awaiting.goal,
            workspace=awaiting.workspace,
            state=awaiting.state,
            created_at=awaiting.created_at,
            updated_at=awaiting.updated_at,
            pending_approval=request,
            pending_clarification=self.pending_clarification,
            tool_executions=self.tool_executions,
            background_processes=self.background_processes,
            project_trust=self.project_trust,
            project_fingerprint=self.project_fingerprint,
            trust_subject=self.trust_subject,
            workspace_baseline=self.workspace_baseline,
            mutation_journal=self.mutation_journal,
            effective_configurations=self.effective_configurations,
            active_agent_checkpoint=self.active_agent_checkpoint,
            onboarding_snapshots=self.onboarding_snapshots,
            active_onboarding_checkpoint=self.active_onboarding_checkpoint,
            session_id=self.session_id,
            workspace_access_grants=self.workspace_access_grants,
        )

    def resolve_approval(self, now: datetime | None = None) -> TaskSnapshot:
        if self.state is not TaskState.AWAITING_APPROVAL or self.pending_approval is None:
            raise ValueError("task does not have a pending approval")
        resumed = self.transition(TaskState.EXECUTING, now)
        return TaskSnapshot(
            task_id=resumed.task_id,
            goal=resumed.goal,
            workspace=resumed.workspace,
            state=resumed.state,
            created_at=resumed.created_at,
            updated_at=resumed.updated_at,
            pending_approval=None,
            pending_clarification=self.pending_clarification,
            tool_executions=self.tool_executions,
            background_processes=self.background_processes,
            project_trust=self.project_trust,
            project_fingerprint=self.project_fingerprint,
            trust_subject=self.trust_subject,
            workspace_baseline=self.workspace_baseline,
            mutation_journal=self.mutation_journal,
            effective_configurations=self.effective_configurations,
            active_agent_checkpoint=self.active_agent_checkpoint,
            onboarding_snapshots=self.onboarding_snapshots,
            active_onboarding_checkpoint=self.active_onboarding_checkpoint,
            session_id=self.session_id,
            workspace_access_grants=self.workspace_access_grants,
        )

    def await_clarification(
        self, request: ClarificationRequest, now: datetime | None = None,
    ) -> TaskSnapshot:
        if self.pending_clarification is not None:
            raise ValueError("task already has a pending clarification")
        if self.pending_approval is not None:
            raise ValueError("task cannot await clarification during approval")
        awaiting = self.transition(TaskState.AWAITING_USER, now)
        return replace(
            awaiting, pending_clarification=request,
            updated_at=now or datetime.now(timezone.utc),
        )

    def resolve_clarification(
        self, now: datetime | None = None,
    ) -> TaskSnapshot:
        if (
            self.state is not TaskState.AWAITING_USER
            or self.pending_clarification is None
        ):
            raise ValueError("task does not have a pending clarification")
        resumed = self.transition(TaskState.EXECUTING, now)
        return replace(resumed, pending_clarification=None)

    def with_tool_execution(
        self, execution: ToolExecutionRecord, now: datetime | None = None
    ) -> TaskSnapshot:
        executions = dict(self.tool_executions)
        executions[execution.execution_id] = execution
        return TaskSnapshot(
            task_id=self.task_id,
            goal=self.goal,
            workspace=self.workspace,
            state=self.state,
            created_at=self.created_at,
            updated_at=now or datetime.now(timezone.utc),
            pending_approval=self.pending_approval,
            pending_clarification=self.pending_clarification,
            tool_executions=executions,
            background_processes=self.background_processes,
            project_trust=self.project_trust,
            project_fingerprint=self.project_fingerprint,
            trust_subject=self.trust_subject,
            workspace_baseline=self.workspace_baseline,
            mutation_journal=self.mutation_journal,
            effective_configurations=self.effective_configurations,
            active_agent_checkpoint=self.active_agent_checkpoint,
            onboarding_snapshots=self.onboarding_snapshots,
            active_onboarding_checkpoint=self.active_onboarding_checkpoint,
            session_id=self.session_id,
            workspace_access_grants=self.workspace_access_grants,
        )

    def with_background_process(
        self, process: BackgroundProcessRecord, now: datetime | None = None
    ) -> TaskSnapshot:
        processes = dict(self.background_processes)
        processes[process.process_id] = process
        return TaskSnapshot(
            task_id=self.task_id, goal=self.goal, workspace=self.workspace,
            state=self.state, created_at=self.created_at,
            updated_at=now or datetime.now(timezone.utc),
            pending_approval=self.pending_approval,
            pending_clarification=self.pending_clarification,
            tool_executions=self.tool_executions, background_processes=processes,
            project_trust=self.project_trust,
            project_fingerprint=self.project_fingerprint,
            trust_subject=self.trust_subject,
            workspace_baseline=self.workspace_baseline,
            mutation_journal=self.mutation_journal,
            effective_configurations=self.effective_configurations,
            active_agent_checkpoint=self.active_agent_checkpoint,
            onboarding_snapshots=self.onboarding_snapshots,
            active_onboarding_checkpoint=self.active_onboarding_checkpoint,
            session_id=self.session_id,
            workspace_access_grants=self.workspace_access_grants,
        )

    def with_project_trust(
        self, level: ProjectTrustLevel, fingerprint: str, subject: str,
        now: datetime | None = None,
    ) -> TaskSnapshot:
        return TaskSnapshot(
            task_id=self.task_id, goal=self.goal, workspace=self.workspace,
            state=self.state, created_at=self.created_at,
            updated_at=now or datetime.now(timezone.utc),
            pending_approval=self.pending_approval,
            pending_clarification=self.pending_clarification,
            tool_executions=self.tool_executions,
            background_processes=self.background_processes,
            project_trust=level, project_fingerprint=fingerprint,
            trust_subject=subject,
            workspace_baseline=self.workspace_baseline,
            mutation_journal=self.mutation_journal,
            effective_configurations=self.effective_configurations,
            active_agent_checkpoint=self.active_agent_checkpoint,
            onboarding_snapshots=self.onboarding_snapshots,
            active_onboarding_checkpoint=self.active_onboarding_checkpoint,
            session_id=self.session_id,
            workspace_access_grants=self.workspace_access_grants,
        )

    def with_workspace_baseline(
        self, baseline: WorkspaceBaseline, now: datetime | None = None
    ) -> TaskSnapshot:
        if baseline.workspace != self.workspace:
            raise ValueError("workspace baseline path does not match task workspace")
        return TaskSnapshot(
            task_id=self.task_id, goal=self.goal, workspace=self.workspace,
            state=self.state, created_at=self.created_at,
            updated_at=now or datetime.now(timezone.utc),
            pending_approval=self.pending_approval,
            pending_clarification=self.pending_clarification,
            tool_executions=self.tool_executions,
            background_processes=self.background_processes,
            project_trust=self.project_trust,
            project_fingerprint=self.project_fingerprint,
            trust_subject=self.trust_subject, workspace_baseline=baseline,
            mutation_journal=self.mutation_journal,
            effective_configurations=self.effective_configurations,
            active_agent_checkpoint=self.active_agent_checkpoint,
            onboarding_snapshots=self.onboarding_snapshots,
            active_onboarding_checkpoint=self.active_onboarding_checkpoint,
            session_id=self.session_id,
            workspace_access_grants=self.workspace_access_grants,
        )

    def with_mutation(
        self, mutation: MutationRecord, now: datetime | None = None
    ) -> TaskSnapshot:
        if any(item.mutation_id == mutation.mutation_id for item in self.mutation_journal):
            raise ValueError(f"duplicate mutation_id: {mutation.mutation_id}")
        return TaskSnapshot(
            task_id=self.task_id, goal=self.goal, workspace=self.workspace,
            state=self.state, created_at=self.created_at,
            updated_at=now or datetime.now(timezone.utc),
            pending_approval=self.pending_approval,
            pending_clarification=self.pending_clarification,
            tool_executions=self.tool_executions,
            background_processes=self.background_processes,
            project_trust=self.project_trust,
            project_fingerprint=self.project_fingerprint,
            trust_subject=self.trust_subject,
            workspace_baseline=self.workspace_baseline,
            mutation_journal=self.mutation_journal + (mutation,),
            effective_configurations=self.effective_configurations,
            active_agent_checkpoint=self.active_agent_checkpoint,
            onboarding_snapshots=self.onboarding_snapshots,
            active_onboarding_checkpoint=self.active_onboarding_checkpoint,
            session_id=self.session_id,
            workspace_access_grants=self.workspace_access_grants,
        )

    def with_effective_configuration(
        self, configuration: EffectiveConfigurationSnapshot,
        now: datetime | None = None,
    ) -> TaskSnapshot:
        expected_revision = len(self.effective_configurations) + 1
        if configuration.revision != expected_revision:
            raise ValueError(
                "effective configuration revision must be monotonic: "
                f"expected {expected_revision}, got {configuration.revision}"
            )
        return TaskSnapshot(
            task_id=self.task_id, goal=self.goal, workspace=self.workspace,
            state=self.state, created_at=self.created_at,
            updated_at=now or datetime.now(timezone.utc),
            pending_approval=self.pending_approval,
            pending_clarification=self.pending_clarification,
            tool_executions=self.tool_executions,
            background_processes=self.background_processes,
            project_trust=self.project_trust,
            project_fingerprint=self.project_fingerprint,
            trust_subject=self.trust_subject,
            workspace_baseline=self.workspace_baseline,
            mutation_journal=self.mutation_journal,
            effective_configurations=self.effective_configurations + (configuration,),
            active_agent_checkpoint=self.active_agent_checkpoint,
            onboarding_snapshots=self.onboarding_snapshots,
            active_onboarding_checkpoint=self.active_onboarding_checkpoint,
            session_id=self.session_id,
            workspace_access_grants=self.workspace_access_grants,
        )

    def with_agent_checkpoint(
        self, checkpoint: Mapping[str, Any] | None,
        now: datetime | None = None,
    ) -> TaskSnapshot:
        return TaskSnapshot(
            task_id=self.task_id, goal=self.goal, workspace=self.workspace,
            state=self.state, created_at=self.created_at,
            updated_at=now or datetime.now(timezone.utc),
            pending_approval=self.pending_approval,
            pending_clarification=self.pending_clarification,
            tool_executions=self.tool_executions,
            background_processes=self.background_processes,
            project_trust=self.project_trust,
            project_fingerprint=self.project_fingerprint,
            trust_subject=self.trust_subject,
            workspace_baseline=self.workspace_baseline,
            mutation_journal=self.mutation_journal,
            effective_configurations=self.effective_configurations,
            active_agent_checkpoint=(
                dict(checkpoint) if checkpoint is not None else None
            ),
            onboarding_snapshots=self.onboarding_snapshots,
            active_onboarding_checkpoint=self.active_onboarding_checkpoint,
            session_id=self.session_id,
            workspace_access_grants=self.workspace_access_grants,
        )

    def with_workspace_access_grant(
        self, grant: WorkspaceAccessGrant, now: datetime | None = None,
    ) -> TaskSnapshot:
        """Persist a non-duplicated Task-only external read grant."""
        if grant.scope != "TASK":
            raise ValueError("only TASK-scoped workspace access is supported")
        if any(
            item.capability is grant.capability
            and item.canonical_root == grant.canonical_root
            for item in self.workspace_access_grants
        ):
            return self
        return replace(
            self,
            workspace_access_grants=self.workspace_access_grants + (grant,),
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_onboarding_checkpoint(
        self, checkpoint: OnboardingCheckpoint | None,
        now: datetime | None = None,
    ) -> TaskSnapshot:
        return replace(
            self, active_onboarding_checkpoint=checkpoint,
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_onboarding_snapshot(
        self, snapshot: ProjectOnboardingSnapshot,
        now: datetime | None = None,
    ) -> TaskSnapshot:
        if any(
            item.snapshot_hash == snapshot.snapshot_hash
            for item in self.onboarding_snapshots
        ):
            return replace(
                self, active_onboarding_checkpoint=None,
                updated_at=now or datetime.now(timezone.utc),
            )
        if (
            snapshot.revision < 1
            or (self.onboarding_snapshots
                and snapshot.revision <= self.onboarding_snapshots[-1].revision)
        ):
            raise ValueError(
                "onboarding revision must increase within a Task"
            )
        return replace(
            self, onboarding_snapshots=self.onboarding_snapshots + (snapshot,),
            active_onboarding_checkpoint=None,
            updated_at=now or datetime.now(timezone.utc),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "goal": self.goal,
            "workspace": self.workspace,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "pending_approval": (
                self.pending_approval.to_data() if self.pending_approval else None
            ),
            "pending_clarification": (
                self.pending_clarification.to_data()
                if self.pending_clarification else None
            ),
            "tool_executions": {
                key: execution.to_data()
                for key, execution in self.tool_executions.items()
            },
            "background_processes": {
                key: process.to_data()
                for key, process in self.background_processes.items()
            },
            "project_trust": self.project_trust.value,
            "project_fingerprint": self.project_fingerprint,
            "trust_subject": self.trust_subject,
            "workspace_baseline": (
                self.workspace_baseline.to_data()
                if self.workspace_baseline is not None else None
            ),
            "mutation_journal": [
                mutation.to_data() for mutation in self.mutation_journal
            ],
            "effective_configurations": [
                configuration.to_data()
                for configuration in self.effective_configurations
            ],
            "active_agent_checkpoint": (
                dict(self.active_agent_checkpoint)
                if self.active_agent_checkpoint is not None else None
            ),
            "onboarding_snapshots": [
                snapshot.to_data() for snapshot in self.onboarding_snapshots
            ],
            "active_onboarding_checkpoint": (
                self.active_onboarding_checkpoint.to_data()
                if self.active_onboarding_checkpoint is not None else None
            ),
            "workspace_access_grants": [
                grant.to_data() for grant in self.workspace_access_grants
            ],
        }
