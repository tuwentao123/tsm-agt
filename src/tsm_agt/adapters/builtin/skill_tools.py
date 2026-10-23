"""Instruction loading and code Skill dispatch through normal tool governance."""

from __future__ import annotations

import inspect
import json
from datetime import datetime

from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.core.skills import SkillKind, SkillRegistry
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ToolCall,
    ToolEffect, ToolIdempotency, ToolInvocationContext, ToolResult,
    ToolResultAuthority, ToolRisk, ToolSpec,
)


class CoreSkillToolProvider:
    descriptor = AdapterDescriptor(
        "builtin.skills", "1.0", "ToolProviderPort", "1.0",
        capabilities=frozenset({"skill.load", "skill.installation_finish"}),
    )

    def __init__(self, registry: SkillRegistry, store: UserSkillsStore, runtime_store=None) -> None:
        self.registry = registry
        self.store = store
        self.runtime_store = runtime_store
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self.store.refresh(self.registry)
        self._started = True

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def health(self) -> HealthStatus:
        return HealthStatus(
            (HealthState.DEGRADED if self.store.issues else HealthState.HEALTHY)
            if self._started else HealthState.UNHEALTHY,
            "; ".join(self.store.issues) or "Skill catalog ready",
        )

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        self.store.refresh(self.registry)
        skills = self.registry.list_all()
        plans = [skill for skill in skills if skill.kind is SkillKind.PLAN]
        tools: list[ToolSpec] = []
        if plans:
            catalog = json.dumps([
                {"name": skill.name, "description": skill.description}
                for skill in plans
            ], ensure_ascii=False)
            tools.append(ToolSpec(
                name="skill.load",
                description=(
                    "Load reusable Skill instructions and then follow them using normal tools. "
                    "When the user names a Skill, load it before performing their task. "
                    "For relevant tasks, choose from this available catalog: " + catalog
                    + ". The optional resource reads a text file relative to the Skill directory."
                ),
                parameters={
                    "type": "object", "properties": {
                        "name": {"type": "string", "enum": [skill.name for skill in plans]},
                        "resource": {"type": "string"},
                    }, "required": ["name"], "additionalProperties": False,
                },
                risk=ToolRisk.R0, is_read_only=True, is_concurrency_safe=True,
                idempotency=ToolIdempotency.IDEMPOTENT, max_result_tokens=100_000,
                effect=ToolEffect.OBSERVE, result_authority=ToolResultAuthority.NONE,
            ))
        for skill in skills:
            if skill.kind is SkillKind.CODE:
                read_only = skill.safety_level == "read_only" and not skill.approval_required
                tools.append(ToolSpec(
                    name=skill.name, description=skill.description,
                    parameters=skill.args_schema, risk=ToolRisk.R0 if read_only else ToolRisk.R2,
                    is_read_only=read_only,
                ))
        if self.runtime_store is not None and self.store.installations.list():
            tools.append(ToolSpec(
                name="skill.installation_finish",
                description="Report actual Skill preparation checks for the installation conversation. Only that conversation can update its receipt. Never report ready before reading SKILL.md and passing required checks.",
                parameters={"type": "object", "properties": {
                    "installation_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["ready", "blocked", "failed"]},
                    "summary": {"type": "string"},
                    "checks": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                }, "required": ["installation_id", "status", "summary", "checks"], "additionalProperties": False},
                risk=ToolRisk.R0, is_read_only=False, is_concurrency_safe=False, is_internal_state=True,
                effect=ToolEffect.INTERNAL, idempotency=ToolIdempotency.IDEMPOTENT,
            ))
        return tuple(tools)

    async def invoke(self, call: ToolCall, context: ToolInvocationContext) -> ToolResult:
        if not self._started:
            raise RuntimeError("Skill provider is not started")
        if call.name == "skill.installation_finish":
            return await self._finish_installation(call, context)
        self.store.refresh(self.registry)
        name = call.arguments.get("name") if call.name == "skill.load" else call.name
        skill = next((item for item in self.registry.list_all() if item.name == name), None)
        if skill is None:
            return ToolResult(call.call_id, False, error_code="SKILL_NOT_FOUND", message=f"Skill unavailable: {name}")
        meta = {"runtime_kind": "skill", "skill_id": skill.id, "skill_kind": skill.kind.value}
        try:
            if call.name == "skill.load" and skill.kind is SkillKind.PLAN:
                resource = call.arguments.get("resource", "SKILL.md")
                if not isinstance(resource, str):
                    raise ValueError("resource must be a string")
                data = self.store.read(skill, resource)
            elif skill.kind is SkillKind.CODE and skill.handler is not None:
                data = skill.handler(call.arguments)
                if inspect.isawaitable(data):
                    data = await data
            else:
                raise ValueError("Use skill.load to load instruction Skills")
            return ToolResult(call.call_id, True, data=data, meta=meta)
        except (OSError, ValueError, UnicodeError) as error:
            return ToolResult(call.call_id, False, error_code="SKILL_LOAD_FAILED", message=str(error), meta=meta)

    async def _finish_installation(self, call: ToolCall, context: ToolInvocationContext) -> ToolResult:
        try:
            if self.runtime_store is None:
                raise ValueError("Installation reporting is unavailable")
            job = self.store.installations.get(call.arguments["installation_id"])
            task = await self.runtime_store.load_task(context.task_id)
            if not task or not job.get("session_id") or task.data.get("session_id") != job["session_id"]:
                raise ValueError("Only the owning installation conversation can report this result")
            status = call.arguments["status"]
            summary, checks = call.arguments["summary"], call.arguments["checks"]
            if status not in {"ready", "blocked", "failed"} or not isinstance(summary, str) or not summary.strip():
                raise ValueError("Provide an installation status and summary")
            if not isinstance(checks, list) or not checks or any(not isinstance(item, str) or not item.strip() for item in checks):
                raise ValueError("Provide actual checks and their results")
            if status == "ready":
                events = await self.runtime_store.read_events(context.task_id)
                loaded = False
                for event in events:
                    result = event.payload.get("result") or {}
                    data = result.get("data")
                    if (event.event_type == "tool.completed" and result.get("ok")
                        and (result.get("meta") or {}).get("runtime_kind") == "skill"
                        and isinstance(data, dict) and data.get("path") == job["plan_path"]):
                        loaded = True
                        break
                if not loaded:
                    raise ValueError("Read the installed SKILL.md using skill.load before reporting ready")
            updated = self.store.installations.update(job["id"], status=status, summary=summary, checks=checks)
            return ToolResult(call.call_id, True, data=updated)
        except (OSError, ValueError, KeyError, StopIteration) as error:
            return ToolResult(call.call_id, False, error_code="SKILL_INSTALLATION_REPORT_FAILED", message=str(error))
