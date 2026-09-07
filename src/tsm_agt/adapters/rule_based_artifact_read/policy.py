"""Conservative, project-neutral repeated artifact read policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tsm_agt.ports import (
    AdapterContext, AdapterDescriptor, ArtifactReadAction,
    ArtifactReadDecision, ArtifactReadPolicyPort, ArtifactReadProbe,
    ArtifactReadRecord, ArtifactReadState, ArtifactReadUpdate, HealthState,
    HealthStatus, ToolCall, ToolResult,
)


class RuleBasedArtifactReadPolicy:
    descriptor = AdapterDescriptor(
        "builtin.rule-based-artifact-read", "1.0.0",
        "ArtifactReadPolicyPort", "1.0",
        frozenset({"project-neutral", "checkpointed", "redacted-events"}),
    )
    _MAX_RECORDS = 1000

    def __init__(self) -> None:
        self._started = False

    async def start(self, context: AdapterContext) -> None:
        self._started = True

    async def health(self) -> HealthStatus:
        return HealthStatus(
            HealthState.HEALTHY if self._started else HealthState.UNHEALTHY,
            "artifact read policy ready" if self._started else "not started",
        )

    async def stop(self, deadline: datetime) -> None:
        self._started = False

    async def before_call(
        self, call: ToolCall, probe: ArtifactReadProbe, state: ArtifactReadState,
    ) -> ArtifactReadDecision:
        self._require_started()
        if call.name != "core.read_file":
            return ArtifactReadDecision(ArtifactReadAction.ALLOW, "not_artifact_read")
        question_hash = self._question_hash(call)
        if not probe.path_hash or not probe.current_content_hash or not question_hash:
            return ArtifactReadDecision(
                ArtifactReadAction.ALLOW, "insufficient_probe",
                path_hash=probe.path_hash, question_hash=question_hash,
            )
        same_artifact = tuple(
            record for record in state.records
            if record.path_hash == probe.path_hash
            and record.content_hash == probe.current_content_hash
        )
        if not same_artifact:
            return ArtifactReadDecision(
                ArtifactReadAction.ALLOW, "new_or_changed_artifact",
                path_hash=probe.path_hash, question_hash=question_hash,
            )
        same_question = tuple(
            record for record in same_artifact
            if record.question_hash == question_hash
        )
        if not same_question:
            return ArtifactReadDecision(
                ArtifactReadAction.ALLOW, "new_evidence_question",
                len(same_artifact), probe.path_hash, question_hash,
            )
        requested_end = probe.start_line + probe.max_lines - 1
        for record in same_question:
            effective_end = min(requested_end, record.total_lines)
            exact_empty_repeat = (
                probe.start_line == record.requested_start_line
                and probe.max_lines == record.requested_max_lines
            )
            covered = (
                probe.start_line >= record.start_line
                and effective_end <= record.end_line
            )
            if exact_empty_repeat or covered:
                return ArtifactReadDecision(
                    ArtifactReadAction.REQUIRE_REUSE, "range_already_read",
                    len(same_question), probe.path_hash, question_hash,
                )
        return ArtifactReadDecision(
            ArtifactReadAction.ALLOW, "new_range", len(same_question),
            probe.path_hash, question_hash,
        )

    async def after_result(
        self, call: ToolCall, result: ToolResult, probe: ArtifactReadProbe,
        state: ArtifactReadState,
    ) -> ArtifactReadUpdate:
        self._require_started()
        if call.name != "core.read_file" or not result.ok:
            return ArtifactReadUpdate(state, "unchanged", len(state.records))
        data = result.data
        question_hash = self._question_hash(call)
        if not isinstance(data, Mapping) or not probe.path_hash or not question_hash:
            return ArtifactReadUpdate(state, "unchanged", len(state.records))
        content_hash = data.get("sha256")
        if not isinstance(content_hash, str) or not content_hash:
            return ArtifactReadUpdate(state, "unchanged", len(state.records))
        try:
            record = ArtifactReadRecord(
                probe.path_hash, content_hash, int(data["start_line"]),
                int(data["end_line"]), max(1, int(data["total_lines"])),
                probe.start_line, probe.max_lines, question_hash,
            )
        except (KeyError, TypeError, ValueError):
            return ArtifactReadUpdate(state, "unchanged", len(state.records))
        records = [item for item in state.records if item != record]
        records.append(record)
        records = records[-self._MAX_RECORDS:]
        return ArtifactReadUpdate(
            ArtifactReadState(tuple(records)), "recorded", len(records)
        )

    @classmethod
    def _question_hash(cls, call: ToolCall) -> str:
        if call.evidence_question is None:
            return ""
        return cls._hash(call.evidence_question.question_id.strip().casefold())

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("artifact read policy is not started")
