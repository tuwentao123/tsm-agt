"""Conservative evidence extraction from provider-neutral structured results."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, EVIDENCE_CATEGORIES, EvidenceDelta,
    EvidenceEvaluation, EvidenceInventory, EvidenceItem, HealthState,
    HealthStatus, ToolCall, ToolResult,
)


class StructuredEvidenceDeltaEvaluator:
    """Extract bounded, deduplicated evidence without project-specific rules."""

    descriptor = AdapterDescriptor(
        "builtin.structured-evidence-delta", "0.1.0",
        "EvidenceDeltaEvaluatorPort", "1.0",
        frozenset({"structured-results", "content-redaction"}),
    )
    _MAX_ITEMS = 1000
    _MAX_INVENTORY_PER_CATEGORY = 5_000
    _MAX_INVENTORY_TOTAL = 10_000
    _PATH_KEYS = frozenset({"path", "file", "filename"})
    _SYMBOL_COLLECTIONS = frozenset({
        "definitions", "implementations", "symbols",
    })
    _RELATION_COLLECTIONS = frozenset({
        "references", "matches", "edits",
    })
    _VOLATILE_KEYS = frozenset({
        "duration", "elapsed", "elapsed_seconds", "finished_at",
        "invocation_id", "pid", "started_at", "timestamp",
    })

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "structured evidence evaluator ready"
            if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def evaluate(
        self, call: ToolCall, result: ToolResult, inventory: EvidenceInventory
    ) -> EvidenceEvaluation:
        if not self._started:
            raise RuntimeError("evidence evaluator is not started")
        candidates = self._extract(call, result) if result.ok else ()
        known = {
            category: set(inventory.fingerprints.get(category, ()))
            for category in EVIDENCE_CATEGORIES
        }
        new_items: list[EvidenceItem] = []
        known_total = sum(len(values) for values in known.values())
        for item in candidates:
            category_known = known[item.category]
            if item.fingerprint in category_known:
                continue
            if len(category_known) >= self._MAX_INVENTORY_PER_CATEGORY:
                continue
            if known_total >= self._MAX_INVENTORY_TOTAL:
                continue
            category_known.add(item.fingerprint)
            known_total += 1
            new_items.append(item)
            if len(new_items) >= self._MAX_ITEMS:
                break
        zero_count = 0 if new_items else inventory.consecutive_zero_delta + 1
        next_inventory = EvidenceInventory(
            {category: tuple(sorted(values)) for category, values in known.items()},
            zero_count,
        )
        question_id = (
            call.evidence_question.question_id.strip()
            if call.evidence_question is not None else ""
        )
        return EvidenceEvaluation(
            EvidenceDelta(
                question_id, tuple(new_items),
                "ok" if result.ok else f"error:{result.error_code or 'UNKNOWN'}",
                zero_count,
            ),
            next_inventory,
        )

    def _extract(
        self, call: ToolCall, result: ToolResult
    ) -> tuple[EvidenceItem, ...]:
        data = result.data
        items: dict[tuple[str, str], EvidenceItem] = {}

        def add(category: str, identity: Any, summary: str) -> None:
            fingerprint = self._hash({"category": category, "identity": identity})
            items[(category, fingerprint)] = EvidenceItem(
                category, fingerprint, summary[:500]
            )

        for path in self._paths(data):
            add("new_paths", path, f"path:{path}")

        if isinstance(data, Mapping):
            for field in self._SYMBOL_COLLECTIONS:
                values = data.get(field)
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                    for value in values:
                        if not isinstance(value, Mapping):
                            continue
                        identity = self._location_identity(value, include_name=True)
                        name = value.get("name") or value.get("symbol") or "symbol"
                        add("new_symbols", identity, f"symbol:{name}@{identity}")
            for field in self._RELATION_COLLECTIONS:
                values = data.get(field)
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                    for value in values:
                        if not isinstance(value, Mapping):
                            continue
                        identity = {
                            "field": field,
                            "location": self._location_identity(value),
                            "query": call.arguments.get("query"),
                            "symbol": call.arguments.get("symbol"),
                        }
                        add(
                            "new_relations", identity,
                            f"{field}:{self._location_identity(value)}",
                        )

        if call.name == "core.read_file" and isinstance(data, Mapping):
            identity = {
                "path": data.get("path"), "sha256": data.get("sha256"),
                "start_line": data.get("start_line"),
                "end_line": data.get("end_line"),
            }
            add(
                "new_facts", identity,
                f"artifact:{data.get('path', '?')} lines "
                f"{data.get('start_line', '?')}-{data.get('end_line', '?')} "
                f"sha256:{str(data.get('sha256', ''))[:16]}",
            )

        if call.name == "core.run_command" and isinstance(data, Mapping):
            exit_code = data.get("exit_code")
            if exit_code is not None:
                identity = {
                    "argv": call.arguments.get("argv"),
                    "cwd": call.arguments.get("cwd", "."),
                    "exit_code": exit_code,
                }
                add(
                    "new_verification", identity,
                    f"command:{self._hash(identity)[:16]} exit_code:{exit_code}",
                )

        if self._is_empty_observation(data):
            identity = {
                "tool": call.name,
                "arguments": self._stable(call.arguments),
            }
            add(
                "new_exclusions", identity,
                f"no-results:{call.name}:{self._hash(identity)[:16]}",
            )
        elif not items and data is not None:
            stable = self._stable(data)
            add(
                "new_facts", stable,
                f"structured-result:{call.name}:{self._hash(stable)[:16]}",
            )

        # A generic evaluator cannot prove that a question is fully resolved.
        # resolved_questions remains empty until a verifier or later semantic layer
        # can make that stronger claim.
        return tuple(items.values())

    @classmethod
    def _paths(cls, value: Any) -> tuple[str, ...]:
        paths: set[str] = set()

        def visit(current: Any) -> None:
            if isinstance(current, Mapping):
                for key, child in current.items():
                    if (
                        str(key).casefold() in cls._PATH_KEYS
                        and isinstance(child, str) and child.strip()
                    ):
                        paths.add(child.strip().replace("\\", "/"))
                    else:
                        visit(child)
            elif isinstance(current, Sequence) and not isinstance(
                current, (str, bytes, bytearray)
            ):
                for child in current:
                    visit(child)

        visit(value)
        return tuple(sorted(paths))

    @staticmethod
    def _location_identity(
        value: Mapping[str, Any], *, include_name: bool = False
    ) -> str:
        parts = [str(value.get("path", "?"))]
        if value.get("line") is not None:
            parts.append(str(value["line"]))
        if value.get("column") is not None:
            parts.append(str(value["column"]))
        if include_name:
            parts.append(str(value.get("name") or value.get("symbol") or "?"))
        return ":".join(parts)

    @classmethod
    def _stable(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            stable: dict[str, Any] = {}
            for key, child in sorted(value.items(), key=lambda item: str(item[0])):
                normalized = str(key).casefold()
                if normalized in cls._VOLATILE_KEYS:
                    continue
                if normalized in {"content", "stderr", "stdout", "text"}:
                    stable[f"{key}_hash"] = cls._hash(child)
                else:
                    stable[str(key)] = cls._stable(child)
            return stable
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [cls._stable(child) for child in value]
        return value

    @staticmethod
    def _is_empty_observation(data: Any) -> bool:
        if not isinstance(data, Mapping):
            return data in (None, "", (), [])
        evidence_fields = (
            "matches", "references", "definitions", "implementations",
            "symbols", "diagnostics", "entries", "edits",
        )
        present = [data[field] for field in evidence_fields if field in data]
        return bool(present) and all(
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes, bytearray))
            and len(value) == 0
            for value in present
        )

    @staticmethod
    def _hash(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
