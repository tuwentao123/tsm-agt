"""Built-in structured workspace mutation tool."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, HealthState, HealthStatus, ToolCall,
    ToolEffect, ToolIdempotency, ToolInvocationContext, ToolResult,
    ToolResultAuthority, ToolRisk, ToolSpec,
)


class CoreWorkspaceMutationToolProvider:
    """Parse model mutation calls and delegate them to a Kernel capability."""

    descriptor = AdapterDescriptor(
        adapter_id="builtin.core-workspace-mutation-tools",
        adapter_version="0.1.0",
        port_name="ToolProviderPort",
        port_version="1.0",
        capabilities=frozenset(
            {
                "core.apply_patch", "core.apply_patches", "core.delete_file",
                "core.rollback_mutation", "core.rollback_mutations",
                "core.rollback_mutation_batch", "core.rollback_mutation_groups",
            }
        ),
    )

    _patch_spec = ToolSpec(
        name="core.apply_patch",
        description=(
            "Apply exact text replacements to one workspace file. Read the file first "
            "and pass its sha256 as expected_hash. Each old_text must occur exactly once "
            "at the time it is applied; ambiguous or stale patches fail without writing. "
            "To create a file, pass expected_hash=null and exactly one edit with empty "
            "old_text. Missing ordinary parent directories are created inside the "
            "same transaction. This R1 action requires approval and is journaled."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "expected_hash": {
                    "anyOf": [{"type": "string"}, {"type": "null"}]
                },
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                        },
                        "required": ["old_text", "new_text"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["path", "expected_hash", "edits"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R1,
        is_read_only=False,
        is_concurrency_safe=False,
        idempotency=ToolIdempotency.NON_IDEMPOTENT,
        rollback="restore the journaled backup only if the current hash still matches the Agent result",
        effect=ToolEffect.MUTATE,
        result_authority=ToolResultAuthority.MUTATION_FACT,
    )
    _patch_many_spec = ToolSpec(
        name="core.apply_patches",
        description=(
            "Apply exact text replacements to 2 to 50 distinct workspace files. "
            "Each item follows core.apply_patch rules and includes a current sha256, "
            "or null when creating it. The runtime validates every patch before "
            "writing, safely creates missing ordinary parent directories, locks all "
            "paths in deterministic order, and commits all "
            "Mutation Journal records together. Ordinary write or journal failures "
            "restore already-written files in reverse order. This R1 action requires "
            "approval of the exact ordered patch payload."
        ),
        parameters={
            "type": "object",
            "properties": {
                "patches": {
                    "type": "array", "minItems": 2, "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "expected_hash": {
                                "anyOf": [{"type": "string"}, {"type": "null"}]
                            },
                            "edits": {
                                "type": "array", "minItems": 1, "maxItems": 100,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "old_text": {"type": "string"},
                                        "new_text": {"type": "string"},
                                    },
                                    "required": ["old_text", "new_text"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["path", "expected_hash", "edits"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["patches"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R1, is_read_only=False, is_concurrency_safe=False,
        idempotency=ToolIdempotency.NON_IDEMPOTENT,
        rollback=(
            "ordinary failures restore completed writes; committed mutations can "
            "later be rolled back from their journal records"
        ),
        effect=ToolEffect.MUTATE,
        result_authority=ToolResultAuthority.MUTATION_FACT,
    )
    _delete_spec = ToolSpec(
        name="core.delete_file",
        description=(
            "Delete one regular workspace file. Read the file first with "
            "core.read_file and pass its whole-file sha256 as expected_hash. "
            "If the file changed, is missing, is sensitive, is a symlink, or is a "
            "directory, deletion fails without removing it. This R1 action requires "
            "approval, creates a backup, and is journaled."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "expected_hash": {"type": "string"},
            },
            "required": ["path", "expected_hash"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R1,
        is_read_only=False,
        is_concurrency_safe=False,
        idempotency=ToolIdempotency.NON_IDEMPOTENT,
        rollback=(
            "restore the journaled backup only while the deleted path is still absent"
        ),
        effect=ToolEffect.MUTATE,
        result_authority=ToolResultAuthority.MUTATION_FACT,
    )
    _rollback_spec = ToolSpec(
        name="core.rollback_mutation",
        description=(
            "Rollback exactly one mutation owned by the current Task. Pass the "
            "mutation_id returned by core.apply_patch or core.delete_file. The runtime "
            "resolves its trusted path and backup from the Mutation Journal. Rollback "
            "fails if the current file state changed, the backup hash is invalid, or "
            "the mutation was already rolled back. This R1 action requires approval "
            "and appends a new journal record; it never erases history."
        ),
        parameters={
            "type": "object",
            "properties": {"mutation_id": {"type": "string"}},
            "required": ["mutation_id"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R1,
        is_read_only=False,
        is_concurrency_safe=False,
        idempotency=ToolIdempotency.NON_IDEMPOTENT,
        rollback=(
            "the rollback itself is journaled; automatic redo is not performed"
        ),
        effect=ToolEffect.MUTATE,
        result_authority=ToolResultAuthority.MUTATION_FACT,
    )
    _rollback_many_spec = ToolSpec(
        name="core.rollback_mutations",
        description=(
            "Rollback 2 to 100 newest active mutations for exactly one file. "
            "Pass mutation_ids in reverse journal order, newest first. The runtime "
            "validates the complete contiguous hash chain and every required backup "
            "before changing the file, then holds one path lock while applying each "
            "rollback. This R1 action requires approval of the exact ordered ID list. "
            "Each rollback is journaled separately; the cascade is sequential, not "
            "a multi-file or all-or-nothing transaction."
        ),
        parameters={
            "type": "object",
            "properties": {
                "mutation_ids": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 100,
                    "items": {"type": "string"},
                },
            },
            "required": ["mutation_ids"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R1,
        is_read_only=False,
        is_concurrency_safe=False,
        idempotency=ToolIdempotency.NON_IDEMPOTENT,
        rollback=(
            "each rollback is journaled separately; automatic cascade redo is not "
            "performed"
        ),
        effect=ToolEffect.MUTATE,
        result_authority=ToolResultAuthority.MUTATION_FACT,
    )
    _rollback_batch_spec = ToolSpec(
        name="core.rollback_mutation_batch",
        description=(
            "Rollback one newest active mutation on each of 2 to 50 distinct files. "
            "All IDs must belong to the current Task. The runtime validates every "
            "file state and backup before writing, locks all paths in deterministic "
            "order, and commits all rollback journal records together. Ordinary "
            "write or journal failures restore already-changed files in reverse "
            "order. This R1 action approves the exact ordered mutation ID list."
        ),
        parameters={
            "type": "object",
            "properties": {
                "mutation_ids": {
                    "type": "array", "minItems": 2, "maxItems": 50,
                    "items": {"type": "string"},
                },
            },
            "required": ["mutation_ids"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R1, is_read_only=False, is_concurrency_safe=False,
        idempotency=ToolIdempotency.NON_IDEMPOTENT,
        rollback=(
            "ordinary failures restore completed rollback writes; committed "
            "rollback records remain auditable and are not automatically redone"
        ),
        effect=ToolEffect.MUTATE,
        result_authority=ToolResultAuthority.MUTATION_FACT,
    )
    _rollback_groups_spec = ToolSpec(
        name="core.rollback_mutation_groups",
        description=(
            "Rollback newest contiguous mutation chains on 2 to 50 distinct files. "
            "Each mutation_ids group represents one file and must be ordered newest "
            "first; the whole request may contain at most 100 unique IDs. The runtime "
            "preflights all files and backups, locks every path deterministically, "
            "changes each file only once to its final restored state, and journals "
            "every reverted mutation. Ordinary failures compensate changed files. "
            "This R1 action approves the exact ordered group payload."
        ),
        parameters={
            "type": "object",
            "properties": {
                "mutation_groups": {
                    "type": "array", "minItems": 2, "maxItems": 50,
                    "items": {
                        "type": "array", "minItems": 1, "maxItems": 100,
                        "items": {"type": "string"},
                    },
                },
            },
            "required": ["mutation_groups"],
            "additionalProperties": False,
        },
        risk=ToolRisk.R1, is_read_only=False, is_concurrency_safe=False,
        idempotency=ToolIdempotency.NON_IDEMPOTENT,
        rollback=(
            "ordinary failures compensate changed files; committed per-mutation "
            "rollback records remain auditable and are not automatically redone"
        ),
        effect=ToolEffect.MUTATE,
        result_authority=ToolResultAuthority.MUTATION_FACT,
    )

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "core workspace mutation tool ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return (
            self._patch_spec, self._patch_many_spec, self._delete_spec,
            self._rollback_spec,
            self._rollback_many_spec, self._rollback_batch_spec,
            self._rollback_groups_spec,
        )

    async def invoke(
        self, call: ToolCall, context: ToolInvocationContext
    ) -> ToolResult:
        if not self._started:
            raise RuntimeError("adapter is not started")
        if call.name not in {
            self._patch_spec.name, self._patch_many_spec.name, self._delete_spec.name,
            self._rollback_spec.name, self._rollback_many_spec.name,
            self._rollback_batch_spec.name, self._rollback_groups_spec.name,
        }:
            return self._error(call, "NOT_FOUND", f"unknown workspace tool: {call.name}")
        control = context.workspace_control
        if control is None:
            return self._error(
                call, "CAPABILITY_UNAVAILABLE",
                "this runtime did not grant task-scoped workspace mutation",
            )
        try:
            if call.name == self._patch_many_spec.name:
                raw_patches = call.arguments.get("patches")
                if not isinstance(raw_patches, (list, tuple)):
                    raise TypeError("patches must be an array")
                if len(raw_patches) < 2 or len(raw_patches) > 50:
                    raise ValueError("patches must contain 2..50 files")
                patches: list[Mapping[str, Any]] = []
                total_edits = 0
                for index, raw_patch in enumerate(raw_patches):
                    if not isinstance(raw_patch, Mapping) or set(raw_patch) != {
                        "path", "expected_hash", "edits"
                    }:
                        raise TypeError(
                            f"patches[{index}] must contain only path, expected_hash "
                            "and edits"
                        )
                    path = raw_patch["path"]
                    if not isinstance(path, str) or not path.strip():
                        raise TypeError(f"patches[{index}].path must be non-empty")
                    expected_hash = raw_patch["expected_hash"]
                    self._validate_expected_hash(expected_hash)
                    edits = self._parse_edits(
                        raw_patch["edits"], f"patches[{index}].edits"
                    )
                    total_edits += len(edits)
                    patches.append({
                        "path": path, "expected_hash": expected_hash,
                        "edits": edits,
                    })
                if total_edits > 500:
                    raise ValueError("patches cannot contain more than 500 edits")
                records = await control.apply_patches(tuple(patches))
                return ToolResult(call.call_id, True, data={
                    "patched": len(records),
                    "mutations": [dict(record) for record in records],
                })
            if call.name == self._rollback_groups_spec.name:
                raw_groups = call.arguments.get("mutation_groups")
                if not isinstance(raw_groups, (list, tuple)):
                    raise TypeError("mutation_groups must be an array")
                if len(raw_groups) < 2 or len(raw_groups) > 50:
                    raise ValueError("mutation_groups must contain 2..50 groups")
                groups: list[tuple[str, ...]] = []
                flattened: list[str] = []
                for index, raw_group in enumerate(raw_groups):
                    if not isinstance(raw_group, (list, tuple)) or not raw_group:
                        raise TypeError(
                            f"mutation_groups[{index}] must be a non-empty array"
                        )
                    if len(raw_group) > 100:
                        raise ValueError(
                            f"mutation_groups[{index}] cannot exceed 100 items"
                        )
                    if any(
                        not isinstance(item, str) or not item.strip()
                        for item in raw_group
                    ):
                        raise TypeError(
                            f"mutation_groups[{index}] items must be non-empty strings"
                        )
                    group = tuple(raw_group)
                    groups.append(group)
                    flattened.extend(group)
                if len(flattened) > 100:
                    raise ValueError(
                        "mutation_groups cannot contain more than 100 IDs"
                    )
                if len(set(flattened)) != len(flattened):
                    raise ValueError("mutation_groups IDs must be unique")
                records = await control.rollback_mutation_groups(tuple(groups))
                return ToolResult(call.call_id, True, data={
                    "rolled_back": len(records),
                    "mutations": [dict(record) for record in records],
                })
            if call.name in {
                self._rollback_many_spec.name, self._rollback_batch_spec.name
            }:
                mutation_ids = call.arguments.get("mutation_ids")
                if not isinstance(mutation_ids, (list, tuple)):
                    raise TypeError("mutation_ids must be an array")
                maximum = 50 if call.name == self._rollback_batch_spec.name else 100
                if len(mutation_ids) < 2 or len(mutation_ids) > maximum:
                    raise ValueError(
                        f"mutation_ids must contain 2..{maximum} items"
                    )
                if any(
                    not isinstance(item, str) or not item.strip()
                    for item in mutation_ids
                ):
                    raise TypeError(
                        "every mutation_ids item must be a non-empty string"
                    )
                if len(set(mutation_ids)) != len(mutation_ids):
                    raise ValueError("mutation_ids must be unique")
                if call.name == self._rollback_batch_spec.name:
                    records = await control.rollback_mutation_batch(
                        tuple(mutation_ids)
                    )
                else:
                    records = await control.rollback_mutations(tuple(mutation_ids))
                return ToolResult(call.call_id, True, data={
                    "rolled_back": len(records),
                    "mutations": [dict(record) for record in records],
                })
            if call.name == self._rollback_spec.name:
                mutation_id = call.arguments.get("mutation_id")
                if not isinstance(mutation_id, str) or not mutation_id.strip():
                    raise TypeError("mutation_id must be a non-empty string")
                data = await control.rollback_mutation(mutation_id)
                return ToolResult(call.call_id, True, data=dict(data))
            path = call.arguments.get("path")
            if not isinstance(path, str) or not path.strip():
                raise TypeError("path must be a non-empty string")
            expected_hash = call.arguments.get("expected_hash")
            self._validate_expected_hash(expected_hash)
            if call.name == self._delete_spec.name:
                if expected_hash is None:
                    raise TypeError(
                        "core.delete_file expected_hash must be a lowercase SHA-256 string"
                    )
                data = await control.delete_file(path, expected_hash)
                return ToolResult(call.call_id, True, data=dict(data))
            edits = self._parse_edits(call.arguments.get("edits"), "edits")
            data = await control.apply_patch(path, expected_hash, tuple(edits))
            return ToolResult(call.call_id, True, data=dict(data))
        except PermissionError as error:
            return self._error(call, "PERMISSION_DENIED", str(error))
        except FileNotFoundError as error:
            return self._error(call, "NOT_FOUND", str(error))
        except (TypeError, ValueError) as error:
            return self._error(call, "INVALID_PARAM", str(error))
        except RuntimeError as error:
            code = "CONFLICT" if error.__class__.__name__ == "WorkspaceMutationConflict" else "TOOL_FAILED"
            return self._error(call, code, str(error))

    @staticmethod
    def _error(call: ToolCall, code: str, message: str) -> ToolResult:
        return ToolResult(call.call_id, False, error_code=code, message=message)

    @staticmethod
    def _validate_expected_hash(expected_hash: Any) -> None:
        if expected_hash is not None and (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or expected_hash != expected_hash.lower()
            or any(character not in "0123456789abcdef" for character in expected_hash)
        ):
            raise TypeError(
                "expected_hash must be a lowercase SHA-256 string or null"
            )

    @staticmethod
    def _parse_edits(raw_edits: Any, label: str) -> tuple[Mapping[str, str], ...]:
        if not isinstance(raw_edits, (list, tuple)) or not raw_edits:
            raise TypeError(f"{label} must be a non-empty array")
        if len(raw_edits) > 100:
            raise ValueError(f"{label} cannot contain more than 100 replacements")
        edits: list[Mapping[str, str]] = []
        for index, edit in enumerate(raw_edits):
            if not isinstance(edit, Mapping) or set(edit) != {"old_text", "new_text"}:
                raise TypeError(
                    f"{label}[{index}] must contain only old_text and new_text"
                )
            old_text, new_text = edit["old_text"], edit["new_text"]
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                raise TypeError(f"{label}[{index}] values must be strings")
            edits.append({"old_text": old_text, "new_text": new_text})
        return tuple(edits)
