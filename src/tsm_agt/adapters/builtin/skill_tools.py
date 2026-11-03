"""Instruction loading and code Skill dispatch through normal tool governance."""

from __future__ import annotations

import inspect
from datetime import datetime

from tsm_agt.ports.skill_files import SkillCatalogPort, SkillFilesPort, SkillResourceError
from tsm_agt.core.skills import SkillKind, SkillRegistry
from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ToolCall,
    ToolEffect, ToolIdempotency, ToolInvocationContext, ToolResult,
    ToolResultAuthority, ToolRisk, ToolSpec, ToolRecoveryKind,
)


class CoreSkillToolProvider:
    descriptor = AdapterDescriptor(
        "builtin.skills", "1.0", "ToolProviderPort", "1.0",
        capabilities=frozenset({"skill.load", "skill.installation_finish"}),
    )

    def __init__(self, registry: SkillRegistry, store: SkillFilesPort, runtime_store=None,
                 *, catalog: SkillCatalogPort) -> None:
        self.registry = registry
        self.store = store
        self.catalog = catalog
        self.runtime_store = runtime_store
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self.refresh_catalog()
        self._started = True

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def health(self) -> HealthStatus:
        return HealthStatus(
            (HealthState.DEGRADED if self.catalog.issues else HealthState.HEALTHY)
            if self._started else HealthState.UNHEALTHY,
            "; ".join(self.catalog.issues) or "Skill catalog ready",
        )

    def refresh_catalog(self) -> tuple:
        return self.catalog.refresh(self.registry)

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        skills = self.refresh_catalog()
        plans = [skill for skill in skills if skill.kind is SkillKind.PLAN]
        tools: list[ToolSpec] = []
        if plans:
            model_data = self.catalog.model_data(skills)
            tools.append(ToolSpec(
                name="skill.load",
                description=(
                    "Load reusable Skill instructions and then follow them using normal tools. "
                    "Load a named or clearly relevant Skill, including verification workflows for implementation tasks. "
                    "Choose from the Available Skills catalog in the request context. "
                    "Omit resource to read SKILL.md. If supplied, resource must be a non-empty "
                    "relative path to a text file within the Skill directory."
                ),
                parameters={
                    "type": "object", "properties": {
                        "name": {"type": "string", "enum": [skill.name for skill in plans]},
                        "resource": {
                            "type": "string", "minLength": 1, "pattern": r"\S",
                            "description": 'Omit resource to read the main instructions, or pass "SKILL.md". '
                            'For a linked file, pass its relative path, e.g. "references/workflow.md". '
                            'Empty or whitespace-only strings are invalid.',
                        },
                        "catalog_revision": {"type": "string"},
                    }, "required": ["name"], "additionalProperties": False,
                },
                risk=ToolRisk.R0, is_read_only=True, is_concurrency_safe=True,
                idempotency=ToolIdempotency.IDEMPOTENT, max_result_tokens=100_000,
                effect=ToolEffect.OBSERVE, result_authority=ToolResultAuthority.NONE,
                model_context=model_data,
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
        skills = self.refresh_catalog()
        expected_revision = call.arguments.get("catalog_revision")
        if expected_revision is not None:
            current = self.catalog.model_data(skills)
            if expected_revision != current["revision"]:
                return ToolResult(call.call_id, False, error_code="SKILL_CATALOG_CHANGED",
                                  message="Skill catalog changed; use the refreshed catalog before loading.", data=current,
                                  hint="Choose the Skill and catalog_revision from the returned catalog, then submit a new call.",
                                  recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE)
        name = call.arguments.get("name") if call.name == "skill.load" else call.name
        skill = next((item for item in self.registry.list_all() if item.name == name), None)
        if skill is None:
            return ToolResult(call.call_id, False, error_code="SKILL_NOT_FOUND", message=f"Skill unavailable: {name}",
                              hint="Choose an exact name from the current Available Skills catalog.",
                              recovery_kind=ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS,
                              data=self.catalog.model_data(skills))
        meta = {"runtime_kind": "skill", "skill_id": skill.id, "skill_kind": skill.kind.value}
        try:
            if call.name == "skill.load" and skill.kind is SkillKind.PLAN:
                resource = call.arguments.get("resource", "SKILL.md")
                data = self.catalog.read(skill, resource)
            elif skill.kind is SkillKind.CODE and skill.handler is not None:
                data = skill.handler(call.arguments)
                if inspect.isawaitable(data):
                    data = await data
            else:
                raise ValueError("Use skill.load to load instruction Skills")
            return ToolResult(call.call_id, True, data=data, meta=meta)
        except SkillResourceError as error:
            recovery = (ToolRecoveryKind.TERMINAL if error.code == "SKILL_RESOURCE_FORBIDDEN"
                        else ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS)
            return self._load_error(call, meta, error.code, str(error), error.hint, recovery)
        except FileNotFoundError as error:
            return self._load_error(call, meta, "SKILL_RESOURCE_NOT_FOUND", str(error),
                                    'Check the relative file path; omit resource or pass "SKILL.md" for the main instructions.',
                                    ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS)
        except IsADirectoryError as error:
            return self._load_error(call, meta, "SKILL_RESOURCE_NOT_FILE", str(error),
                                    "Pass a file path instead of a directory.",
                                    ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS)
        except PermissionError as error:
            return self._load_error(call, meta, "SKILL_RESOURCE_PERMISSION_DENIED", str(error),
                                    "The resource is not readable. Ask its owner to fix file permissions; do not retry unchanged or bypass access controls.",
                                    ToolRecoveryKind.USER_ACTION_REQUIRED)
        except UnicodeError:
            return self._load_error(call, meta, "SKILL_RESOURCE_ENCODING_ERROR",
                                    "Skill resource is not valid UTF-8 text.",
                                    "Use a UTF-8 text resource or ask its owner to convert the file before retrying.",
                                    ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE)
        except OSError as error:
            return self._load_error(call, meta, "SKILL_RESOURCE_READ_FAILED", str(error),
                                    "Inspect the file or installation error before retrying; repeating the same call may not help.",
                                    ToolRecoveryKind.TERMINAL)
        except ValueError as error:
            # Preserve a conservative fallback for code Skills and third-party
            # catalogs without guessing recovery semantics from error prose.
            return self._load_error(call, meta, "SKILL_LOAD_FAILED", str(error),
                                    "Inspect the reported Skill error before choosing another action.",
                                    ToolRecoveryKind.TERMINAL)

    @staticmethod
    def _load_error(call, meta, code, message, hint, recovery) -> ToolResult:
        return ToolResult(
            call.call_id, False, error_code=code, message=message, hint=hint, meta=meta,
            recovery_kind=recovery,
            recovery_action={"field": "resource", "skill": call.arguments.get("name"),
                             "resource": call.arguments.get("resource", "SKILL.md")},
        )

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
