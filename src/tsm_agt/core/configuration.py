"""Immutable, redacted Effective Configuration snapshots."""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tsm_agt.ports import AdapterDescriptor, HealthStatus, ProviderCapabilities, ToolSpec


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def effective_toolset_hash(tools: tuple[ToolSpec, ...]) -> str:
    return canonical_hash(tuple(sorted(
        (EffectiveConfigurationSnapshot._tool_data(tool) for tool in tools),
        key=lambda item: item["name"],
    )))


@dataclass(frozen=True, slots=True)
class EffectiveConfigurationSnapshot:
    schema_version: int
    revision: int
    captured_at: datetime
    model: Mapping[str, Any]
    provider_capabilities: Mapping[str, bool | int]
    execution: Mapping[str, Any]
    tools: tuple[Mapping[str, Any], ...]
    adapters: tuple[Mapping[str, Any], ...]
    policy: Mapping[str, Any]
    project: Mapping[str, Any]
    prompt_manifest: Mapping[str, Any]
    extensions: tuple[Mapping[str, Any], ...]
    workflow: Mapping[str, Any]
    environment: Mapping[str, Any]
    sources: Mapping[str, str]
    toolset_hash: str
    adapter_lock_hash: str
    policy_hash: str
    provider_capabilities_hash: str
    prompt_manifest_hash: str | None
    effective_config_hash: str

    @classmethod
    def build(
        cls, *, captured_at: datetime, model: Mapping[str, Any],
        capabilities: ProviderCapabilities,
        tools: tuple[ToolSpec, ...],
        adapters: tuple[tuple[AdapterDescriptor, HealthStatus], ...],
        policy: Mapping[str, Any], project: Mapping[str, Any],
        sources: Mapping[str, str], revision: int = 1,
        prompt_manifest: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> EffectiveConfigurationSnapshot:
        tool_data = tuple(sorted(
            (cls._tool_data(tool) for tool in tools), key=lambda item: item["name"]
        ))
        adapter_data = tuple(sorted(
            (cls._adapter_data(descriptor, health)
             for descriptor, health in adapters),
            key=lambda item: (item["port_name"], item["adapter_id"]),
        ))
        capability_data = capabilities.to_data()
        prompt_manifest_data: Mapping[str, Any] = prompt_manifest or {
            "status": "not_configured", "manifest_id": None,
            "revision": None,
        }
        execution = {
            "mode": "serial-agent-loop",
            "parallel_tools": capabilities.parallel_tools,
            "checkpoint_resume": True,
            "context": dict(context or {"status": "not_configured"}),
        }
        environment = {
            "os_family": platform.system() or "unknown",
            "machine": platform.machine() or "unknown",
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
        }
        extensions: tuple[Mapping[str, Any], ...] = ()
        workflow = {"status": "not_selected", "workflow_id": None, "version": None}
        hashes = {
            "toolset_hash": effective_toolset_hash(tools),
            "adapter_lock_hash": canonical_hash(adapter_data),
            "policy_hash": canonical_hash(policy),
            "provider_capabilities_hash": canonical_hash(capability_data),
            "prompt_manifest_hash": (
                str(prompt_manifest_data["manifest_hash"])
                if prompt_manifest_data.get("manifest_hash") is not None else None
            ),
        }
        content = {
            "schema_version": 1, "revision": revision,
            "model": dict(model),
            "provider_capabilities": capability_data,
            "execution": execution,
            "tools": tool_data, "adapters": adapter_data,
            "policy": dict(policy), "project": dict(project),
            "prompt_manifest": dict(prompt_manifest_data),
            "extensions": extensions, "workflow": workflow,
            "environment": environment, "sources": dict(sources),
            **hashes,
        }
        return cls(
            captured_at=captured_at, effective_config_hash=canonical_hash(content),
            **content,
        )

    @staticmethod
    def _tool_data(tool: ToolSpec) -> dict[str, Any]:
        return {
            "name": tool.name,
            "parameters_hash": canonical_hash(dict(tool.parameters)),
            "risk": tool.risk.value,
            "is_read_only": tool.is_read_only,
            "is_internal_state": tool.is_internal_state,
            "is_concurrency_safe": tool.is_concurrency_safe,
            "idempotency": tool.idempotency.value,
            "max_result_tokens": tool.max_result_tokens,
            "requires_network": tool.requires_network,
            "data_transmission": tool.data_transmission,
        }

    @staticmethod
    def _adapter_data(
        descriptor: AdapterDescriptor, health: HealthStatus,
    ) -> dict[str, Any]:
        return {
            "adapter_id": descriptor.adapter_id,
            "adapter_version": descriptor.adapter_version,
            "port_name": descriptor.port_name,
            "port_version": descriptor.port_version,
            "capabilities": sorted(descriptor.capabilities),
            "health": health.state.value,
        }

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "captured_at": self.captured_at.isoformat(),
            "model": dict(self.model),
            "provider_capabilities": dict(self.provider_capabilities),
            "execution": dict(self.execution),
            "tools": [dict(item) for item in self.tools],
            "adapters": [dict(item) for item in self.adapters],
            "policy": dict(self.policy), "project": dict(self.project),
            "prompt_manifest": dict(self.prompt_manifest),
            "extensions": [dict(item) for item in self.extensions],
            "workflow": dict(self.workflow),
            "environment": dict(self.environment),
            "sources": dict(self.sources),
            "toolset_hash": self.toolset_hash,
            "adapter_lock_hash": self.adapter_lock_hash,
            "policy_hash": self.policy_hash,
            "provider_capabilities_hash": self.provider_capabilities_hash,
            "prompt_manifest_hash": self.prompt_manifest_hash,
            "effective_config_hash": self.effective_config_hash,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> EffectiveConfigurationSnapshot:
        def mapping(name: str) -> Mapping[str, Any]:
            value = data.get(name)
            if not isinstance(value, Mapping):
                raise ValueError(f"effective configuration {name} must be an object")
            return dict(value)

        def sequence(name: str) -> tuple[Mapping[str, Any], ...]:
            value = data.get(name)
            if not isinstance(value, list) or not all(
                isinstance(item, Mapping) for item in value
            ):
                raise ValueError(f"effective configuration {name} must be an array")
            return tuple(dict(item) for item in value)

        if data.get("schema_version") != 1:
            raise ValueError("unsupported effective configuration schema version")
        return cls(
            schema_version=1, revision=int(data["revision"]),
            captured_at=datetime.fromisoformat(str(data["captured_at"])),
            model=mapping("model"),
            provider_capabilities=mapping("provider_capabilities"),
            execution=mapping("execution"), tools=sequence("tools"),
            adapters=sequence("adapters"), policy=mapping("policy"),
            project=mapping("project"), prompt_manifest=mapping("prompt_manifest"),
            extensions=sequence("extensions"), workflow=mapping("workflow"),
            environment=mapping("environment"), sources=mapping("sources"),
            toolset_hash=str(data["toolset_hash"]),
            adapter_lock_hash=str(data["adapter_lock_hash"]),
            policy_hash=str(data["policy_hash"]),
            provider_capabilities_hash=str(data["provider_capabilities_hash"]),
            prompt_manifest_hash=(
                str(data["prompt_manifest_hash"])
                if data.get("prompt_manifest_hash") is not None else None
            ),
            effective_config_hash=str(data["effective_config_hash"]),
        )
