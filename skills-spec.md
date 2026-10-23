# Skill Runtime Spec

This document captures the initial implementation spec for skill definition,
registration, discovery, and execution in `tsm-agt`. It is intended to be
"implementation ready": every type and function mentioned here should map
directly to concrete code.

## 1. Skill core types and registry

Module: `src/tsm_agt/core/skills.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Mapping


class SkillKind(StrEnum):
    CODE = "code"   # Python handler
    PLAN = "plan"   # SKILL.md / plan-based


@dataclass(frozen=True, slots=True)
class Skill:
    id: str                    # global id, e.g. "code.search_files"
    name: str                  # tool name exposed to the model
    description: str           # short description for the model
    kind: SkillKind

    # JSON-schema-like argument and output contracts
    args_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None

    # CODE skills: a Python callable; PLAN skills: None
    handler: Callable[[Mapping[str, Any]], Any] | None

    # PLAN skills: path to SKILL.md or similar; CODE skills: None
    plan_path: str | None

    # Metadata
    tags: tuple[str, ...]
    safety_level: str          # e.g. "read_only", "write_local", ...
    approval_required: bool
```

The registry manages the global skill catalog and session bindings:

```python
from dataclasses import dataclass, field
from collections.abc import Iterable


@dataclass
class SkillRegistry:
    _skills: dict[str, Skill] = field(default_factory=dict)
    _session_enabled: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def register(self, skill: Skill) -> None: ...
    def get(self, skill_id: str) -> Skill | None: ...
    def list_all(self) -> tuple[Skill, ...]: ...
    def list_by_tags(self, tags: Iterable[str]) -> tuple[Skill, ...]: ...

    def set_session_enabled(self, session_id: str, skill_ids: tuple[str, ...]) -> None: ...
    def get_session_enabled(self, session_id: str) -> tuple[str, ...]: ...
    def list_session_skills(self, session_id: str) -> tuple[Skill, ...]: ...
```

`KernelDependencies` will own a singleton `SkillRegistry` instance.

## 2. Session-wide skill configuration

Module: `src/tsm_agt/core/session.py`

`SessionSnapshot` is extended with a Session-wide skill list:

```python
@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    ...
    reasoning_effort: str | None = None
    enabled_skills: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        session_id: str,
        subject: str,
        title: str,
        now: datetime | None = None,
        reasoning_effort: str | None = None,
        enabled_skills: tuple[str, ...] = (),
    ) -> SessionSnapshot:
        ...
```

Serialization is updated accordingly:

```python
def to_data(self) -> dict[str, Any]:
    return {
        ...,
        "reasoning_effort": self.reasoning_effort,
        "enabled_skills": list(self.enabled_skills),
    }

@classmethod
def from_data(cls, data: Mapping[str, Any]) -> SessionSnapshot:
    return cls(
        ...,
        reasoning_effort=data.get("reasoning_effort"),
        enabled_skills=tuple(str(sid) for sid in data.get("enabled_skills", [])),
    )
```

Module: `src/tsm_agt/core/kernel_session.py`

`SessionMixin` gains a method to update the Session-wide skill list:

```python
class SessionMixin:
    async def update_session_skills(
        self,
        session_id: str,
        enabled_skill_ids: tuple[str, ...],
    ) -> SessionSnapshot:
        """Update the Session-wide skill ids.

        The Session remains the single source of truth; this method validates
        skill ids against SkillRegistry and emits a `session.skills_updated`
        event.
        """
        ...
```

This method:

1. Loads the session snapshot from the store.
2. Enforces subject isolation (same check as `get_session`).
3. Validates `enabled_skill_ids` via `SkillRegistry.get`.
4. Writes a new snapshot with `enabled_skills` and bumped `updated_at`.
5. Commits a `SessionUnitOfWork` with `event_type="session.skills_updated"`.
6. Updates `SkillRegistry.set_session_enabled(session_id, enabled_skill_ids)`.

`create_session` initializes `enabled_skills=()` and registers that in the
registry as well.

## 3. Turn-level checkpoint view

Module: `src/tsm_agt/core/agent_loop.py`

`AgentTurnCheckpoint` gains a per-turn list of enabled skill ids:

```python
@dataclass(frozen=True, slots=True)
class AgentTurnCheckpoint:
    ...
    enabled_skills: tuple[str, ...] = ()
```

`TurnState` also carries this list for convenience during tool execution:

```python
@dataclass
class TurnState:
    ...
    enabled_skills: tuple[str, ...] = ()
```

Module: `src/tsm_agt/core/kernel_checkpoint.py`

`CheckpointMixin._bind_agent_checkpoint` is extended so that when an
`AgentTurnCheckpoint` does not already declare `enabled_skills`, the runtime
defaults them from the owning `SessionSnapshot.enabled_skills`:

```python
async def _bind_agent_checkpoint(
    self,
    task: TaskSnapshot,
    checkpoint: AgentTurnCheckpoint,
) -> AgentTurnCheckpoint:
    ...
    session = await self.get_session(task.session_id)
    if not checkpoint.enabled_skills:
        checkpoint = replace(
            checkpoint,
            enabled_skills=session.enabled_skills,
        )
    ...
    return checkpoint
```

The rest of the method is unchanged.

## 4. SkillSelector: per-turn discovery

Module: `src/tsm_agt/core/skill_selector.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .session import SessionSnapshot
from .skills import SkillRegistry


@dataclass
class SkillSelector:
    registry: SkillRegistry

    async def select_for_turn(
        self,
        session: SessionSnapshot,
        user_text: str,
        *,
        max_skills: int = 8,
    ) -> tuple[str, ...]:
        """Return a subset of Session-enabled skills for this turn.

        Initial implementation uses simple keyword and tag matching, falling
        back to the Session-wide list truncated to `max_skills`.
        """
        ...
```

Logic (first implementation):

1. Start from `session.enabled_skills` as the candidate id set.
2. For each candidate, compute a simple score:
   * +5 if any tag appears in `user_text.lower()`.
   * +3 if the skill name appears in `user_text.lower()`.
   * +1 if the description contains generic words like "search", "code",
     "test", or "file".
3. Sort by score descending.
4. If all scores are zero, fallback to `session.enabled_skills[:max_skills]`.
5. Otherwise return the highest scoring `max_skills` ids.

`KernelDependencies` will expose a `SkillSelector` instance.

## 5. ConversationAgentLoop: exposing skills as tools

Module: `src/tsm_agt/core/conversation_loop/loop.py`

In `ConversationAgentLoop.run`, after obtaining `visible_tools` but before
calling `select_model_tools`, the loop performs per-turn skill discovery and
adds them to the tool list:

```python
visible_tools = await kernel.list_tools()

session = await kernel.get_session(task.session_id)
registry = kernel._dependencies.skill_registry
selector = kernel._dependencies.skill_selector

enabled_skill_ids = await selector.select_for_turn(
    session,
    user_text=user_text,
)

# TODO: ensure the turn checkpoint carries enabled_skills; this is handled in
# the checkpoint binding logic (see §3).

skill_specs: list[ToolSpec] = []
for skill_id in enabled_skill_ids:
    skill = registry.get(skill_id)
    if skill is None:
        continue
    skill_specs.append(ToolSpec(
        name=skill.name,
        description=skill.description,
        parameters=skill.args_schema,
        risk=ToolRisk.LOW,  # or derived from skill.safety_level
    ))

visible_tools = tuple(list(visible_tools) + skill_specs)

initial_model_tools = select_model_tools(
    visible_tools,
    user_text=user_text,
    model_call=1,
    task_kind=task.task_kind,
)
```

Event payloads that log `visible_tools` continue to use `initial_model_tools`.

## 6. Tool execution: mapping tool calls back to skills

Module: `src/tsm_agt/core/turn_tool_batch.py`

`run_tool_batch` is extended so that tool calls whose names correspond to a
registered `Skill` are executed via the skill runtime rather than a regular
tool provider.

High-level shape:

```python
from .skills import SkillRegistry, SkillKind


async def run_tool_batch(kernel: Any, state: TurnState, ...) -> ...:
    ...
    while state.pending_tool_calls:
        ...  # existing outcome binding and governance logic

        call = state.pending_tool_calls[0]
        call_spec = state.visible_tool_by_name.get(call.name)

        # Skill dispatch (before regular tool execution)
        registry: SkillRegistry = kernel._dependencies.skill_registry
        skill = next(
            (s for s in registry.list_all() if s.name == call.name),
            None,
        )

        if skill is not None:
            args = call.arguments
            try:
                if skill.kind is SkillKind.CODE and skill.handler is not None:
                    raw_result = await _maybe_async_call(skill.handler, args)
                elif skill.kind is SkillKind.PLAN and skill.plan_path is not None:
                    raw_result = await kernel._execute_plan_skill(skill, args, state)
                else:
                    raise RuntimeError(f"skill not executable: {skill.id}")

                result = ToolResult(
                    call.call_id,
                    True,
                    error_code=None,
                    message="",
                    hint=None,
                    meta={},
                    result_data=raw_result,
                )
            except Exception as exc:
                result = ToolResult(
                    call.call_id,
                    False,
                    error_code="SKILL_EXECUTION_ERROR",
                    message=str(exc),
                    hint=None,
                    meta={},
                )

            governed = _govern_runtime_result(kernel, state, call, result)
            # Existing post-processing path (events, checkpoint updates, etc.)
            ...
            continue

        # Non-skill tools follow the existing path
        ...
```

Helper:

```python
import inspect


async def _maybe_async_call(handler, args):
    result = handler(args)
    if inspect.isawaitable(result):
        return await result
    return result
```

Kernel will gain a private method `_execute_plan_skill` for PLAN-type skills.
Its first implementation can be a placeholder that raises a clear error until
plan execution is implemented.

## 7. Wiring into KernelDependencies and composition

Module: `src/tsm_agt/core/__init__.py` / `src/tsm_agt/core/kernel.py`

`KernelDependencies` gains two fields:

```python
from .skills import SkillRegistry
from .skill_selector import SkillSelector


@dataclass(frozen=True, slots=True)
class KernelDependencies:
    ...
    skill_registry: SkillRegistry
    skill_selector: SkillSelector
```

Module: `src/tsm_agt/bootstrap/composition.py`

The application composition root constructs and injects these dependencies:

```python
from tsm_agt.core.skills import SkillRegistry
from tsm_agt.core.skill_selector import SkillSelector


def build_kernel(...):
    skill_registry = SkillRegistry()
    skill_selector = SkillSelector(skill_registry)

    deps = KernelDependencies(
        ...,
        skill_registry=skill_registry,
        skill_selector=skill_selector,
    )
    kernel = Kernel(deps)
    return kernel
```

Future work such as auto-registering skills from SKILL.md or loading
configuration-driven skills builds on top of this spec.

