"""Provider-neutral contracts for bounded artifact read reuse."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .adapter import RuntimeAdapter
from .tool import ToolCall, ToolResult


class ArtifactReadAction(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_REUSE = "REQUIRE_REUSE"


@dataclass(frozen=True, slots=True)
class ArtifactReadProbe:
    path_hash: str = ""
    current_content_hash: str = ""
    start_line: int = 1
    max_lines: int = 200


@dataclass(frozen=True, slots=True)
class ArtifactReadRecord:
    path_hash: str
    content_hash: str
    start_line: int
    end_line: int
    total_lines: int
    requested_start_line: int
    requested_max_lines: int
    question_hash: str

    def __post_init__(self) -> None:
        if not self.path_hash or not self.content_hash or not self.question_hash:
            raise ValueError("artifact read record hashes must not be empty")
        if min(
            self.start_line, self.total_lines, self.requested_start_line,
            self.requested_max_lines,
        ) < 1:
            raise ValueError("artifact read record line values must be positive")
        if self.end_line < self.start_line - 1:
            raise ValueError("artifact read record end_line is invalid")

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ArtifactReadRecord:
        return cls(
            str(data.get("path_hash", "")),
            str(data.get("content_hash", "")),
            int(data.get("start_line", 1)),
            int(data.get("end_line", 0)),
            int(data.get("total_lines", 1)),
            int(data.get("requested_start_line", 1)),
            int(data.get("requested_max_lines", 200)),
            str(data.get("question_hash", "")),
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "path_hash": self.path_hash,
            "content_hash": self.content_hash,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "total_lines": self.total_lines,
            "requested_start_line": self.requested_start_line,
            "requested_max_lines": self.requested_max_lines,
            "question_hash": self.question_hash,
        }


@dataclass(frozen=True, slots=True)
class ArtifactReadState:
    records: tuple[ArtifactReadRecord, ...] = ()

    @classmethod
    def from_data(cls, data: Mapping[str, Any] | None) -> ArtifactReadState:
        if not data:
            return cls()
        raw = data.get("records", ())
        if not isinstance(raw, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in raw
        ):
            raise ValueError("artifact read records must be an array of objects")
        return cls(tuple(ArtifactReadRecord.from_data(item) for item in raw))

    def to_data(self) -> dict[str, Any]:
        return {"records": [record.to_data() for record in self.records]}


@dataclass(frozen=True, slots=True)
class ArtifactReadDecision:
    action: ArtifactReadAction
    reason: str
    matching_record_count: int = 0
    path_hash: str = ""
    question_hash: str = ""


@dataclass(frozen=True, slots=True)
class ArtifactReadUpdate:
    state: ArtifactReadState
    transition: str
    record_count: int


class ArtifactReadPolicyPort(RuntimeAdapter, Protocol):
    async def before_call(
        self, call: ToolCall, probe: ArtifactReadProbe, state: ArtifactReadState,
    ) -> ArtifactReadDecision: ...

    async def after_result(
        self, call: ToolCall, result: ToolResult, probe: ArtifactReadProbe,
        state: ArtifactReadState,
    ) -> ArtifactReadUpdate: ...
