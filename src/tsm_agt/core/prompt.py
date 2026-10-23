"""Deterministic prompt assembly without persisting prompt bodies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
import json
import re
from typing import Any

from tsm_agt.ports import Message, MessageRole, TextBlock, ToolResultBlock, ToolSpec

from .configuration import canonical_hash
from .session_history import render_session_history


_PROMPT_MARKER = re.compile(r"^<!-- prompt: ([a-z0-9-]+) -->$", re.MULTILINE)
_PROMPT_FILES = (
    "base.md", "communication.md", "workspace.md", "external.md",
    "coordination.md",
)


def _load_prompt_text() -> dict[str, str]:
    """Read trusted packaged prompt sections, preserving their model-visible text."""
    root = files("tsm_agt.core").joinpath("prompt_templates")
    # Keep the file order explicit: changing a stable prefix defeats provider
    # prompt caching even if the same instruction sections are still present.
    text = "\n".join(
        root.joinpath(name).read_text(encoding="utf-8") for name in _PROMPT_FILES
    )
    markers = list(_PROMPT_MARKER.finditer(text))
    if not markers or text[:markers[0].start()].strip():
        raise ValueError("invalid packaged prompt sections")
    sections: dict[str, str] = {}
    for index, marker in enumerate(markers):
        name = marker.group(1)
        if name in sections:
            raise ValueError(f"duplicate packaged prompt section: {name}")
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        lines = text[marker.end():end].strip("\n").split("\n")
        # Markdown soft-wrapped prose is joined with spaces; blank lines and
        # headings retain their explicit breaks in the model-visible content.
        body = lines[0]
        for previous, line in zip(lines, lines[1:]):
            separator = (
                "\n" if not previous or not line
                or previous.startswith("# ") or line.startswith("# ") else " "
            )
            body += separator + line
        if not body.strip():
            raise ValueError(f"empty packaged prompt section: {name}")
        sections[name] = body
    return sections


@dataclass(frozen=True, slots=True)
class PromptTemplateSegment:
    segment_id: str
    source: str
    version: str
    content: str
    trust: str = "trusted_instruction"
    #: Capability gate for the stable prompt. When this is non-empty the segment
    #: is assembled only if the turn advertises at least one of these tools, so
    #: the prompt never names a capability the Runtime did not expose.
    requires_any_tools: tuple[str, ...] = ()
    def __post_init__(self) -> None:
        if not all((
            self.segment_id.strip(), self.source.strip(), self.version.strip(),
            self.content.strip(), self.trust.strip(),
        )):
            raise ValueError("prompt template segment fields must not be empty")
        if any(not name.strip() for name in self.requires_any_tools):
            raise ValueError("prompt template capability gate names must not be empty")

    def applies_to(self, visible_tool_names: frozenset[str]) -> bool:
        if not self.requires_any_tools:
            return True
        return bool(visible_tool_names.intersection(self.requires_any_tools))

    def manifest_data(self, ordinal: int) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ordinal": ordinal,
            "segment_id": self.segment_id,
            "source": self.source,
            "version": self.version,
            "role": MessageRole.SYSTEM.value,
            "trust": self.trust,
            "content_hash": canonical_hash(self.content),
            "token_count": None,
            "token_count_source": "provider_tokenizer_unavailable",
        }
        if self.requires_any_tools:
            data["requires_any_tools"] = list(self.requires_any_tools)
        return data


@dataclass(frozen=True, slots=True)
class PromptManifest:
    schema_version: int
    manifest_id: str
    revision: int
    assembly_order: tuple[str, ...]
    static_segments: tuple[Mapping[str, Any], ...]
    manifest_hash: str

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": "configured",
            "manifest_id": self.manifest_id,
            "revision": self.revision,
            "assembly_order": list(self.assembly_order),
            "static_segments": [dict(item) for item in self.static_segments],
            "manifest_hash": self.manifest_hash,
        }


@dataclass(frozen=True, slots=True)
class PromptAssemblyReceipt:
    manifest_hash: str
    effective_prompt_hash: str
    message_count: int
    static_segment_count: int
    conversation_message_count: int
    tool_count: int
    toolset_hash: str
    role_order: tuple[str, ...]
    static_segment_ids: tuple[str, ...]
    tool_names: tuple[str, ...]
    stable_prefix_hash: str = ""
    ordered_toolset_hash: str = ""
    message_structure: tuple[Mapping[str, Any], ...] = ()

    def event_data(self) -> dict[str, Any]:
        """Payload-safe facts; deliberately excludes messages and segment text."""
        return {
            "prompt_manifest_hash": self.manifest_hash,
            "effective_prompt_hash": self.effective_prompt_hash,
            "prompt_message_count": self.message_count,
            "prompt_static_segment_count": self.static_segment_count,
            "prompt_conversation_message_count": self.conversation_message_count,
            "prompt_tool_count": self.tool_count,
            "prompt_toolset_hash": self.toolset_hash,
            "prompt_role_order": list(self.role_order),
            "prompt_static_segment_ids": list(self.static_segment_ids),
            "prompt_tool_names": list(self.tool_names),
            "prompt_stable_prefix_hash": self.stable_prefix_hash,
            "prompt_ordered_toolset_hash": self.ordered_toolset_hash,
            # This is the provider-neutral request, not the adapter's wire
            # payload: adapters may add or transform provider-specific messages.
            "prompt_message_structure_stage": "provider_neutral_after_compaction",
            "prompt_message_structure": [dict(item) for item in self.message_structure],
        }


@dataclass(frozen=True, slots=True)
class PromptAssembly:
    messages: tuple[Message, ...]
    receipt: PromptAssemblyReceipt


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    manifest_id: str
    revision: int
    static_segments: tuple[PromptTemplateSegment, ...]

    @classmethod
    def default(cls) -> PromptTemplate:
        prompt_text = _load_prompt_text()
        template = cls(
            manifest_id="builtin.engineering-agent",
            revision=42,
            static_segments=(
                PromptTemplateSegment(
                    "system-safety", "core", "1.0",
                    prompt_text["system-safety"],
                ),
                # The pipeline runs on every turn and before tool choice: it is
                # the only place that says how a request becomes an answer, so a
                # capability-gated segment never has to carry routing rules.
                PromptTemplateSegment(
                    "request-pipeline-guidance", "harness", "4.5",
                    prompt_text["request-pipeline-guidance"],
                ),
                # This is deliberately a stable, tool-capability-gated contract:
                # the model determines whether the request is implementation
                # work from its full conversational context.  Runtime does not
                # classify user text or inject a per-turn implementation label.
                PromptTemplateSegment(
                    "delivery-mode-guidance", "harness", "4.5",
                    prompt_text["delivery-mode-guidance"],
                    requires_any_tools=(
                        "core.apply_patch", "core.apply_patch_v4a",
                        "core.apply_patches", "core.delete_file",
                    ),
                ),
                # Cross-tool work-loop guidance in the stable base layer: it
                # applies to every advertised tool, so it is ungated and must
                # not duplicate the command-specific facts kept in
                # workspace-command-guidance.
                PromptTemplateSegment(
                    "tool-feedback-guidance", "harness", "1.3",
                    prompt_text["tool-feedback-guidance"],
                ),
                PromptTemplateSegment(
                    "response-format-guidance", "harness", "1.6",
                    prompt_text["response-format-guidance"],
                ),
                PromptTemplateSegment(
                    "execution-discipline", "harness", "1.3",
                    prompt_text["execution-discipline"],
                ),
                PromptTemplateSegment(
                    "completion-behavior-examples", "harness", "3.2",
                    prompt_text["completion-behavior-examples"],
                ),
                # ---- tool usage: one segment per capability, ordered by what
                # the segment teaches (workspace, another project's source,
                # external documents, live facts, runtime facts) ----
                PromptTemplateSegment(
                    "workspace-search-guidance", "harness", "2.1",
                    prompt_text["workspace-search-guidance"],
                    requires_any_tools=("core.search_files",),
                ),
                PromptTemplateSegment(
                    "workspace-read-guidance", "harness", "1.0",
                    prompt_text["workspace-read-guidance"],
                    requires_any_tools=("core.read_file", "core.search_files"),
                ),
                PromptTemplateSegment(
                    "code-edit-guidance", "harness", "1.6",
                    prompt_text["code-edit-guidance"],
                    requires_any_tools=("core.apply_patch", "core.apply_patch_v4a"),
                ),
                PromptTemplateSegment(
                    "workspace-command-guidance", "harness", "1.5",
                    prompt_text["workspace-command-guidance"],
                    requires_any_tools=("core.run_command", "core.process_wait"),
                ),
                PromptTemplateSegment(
                    "git-read-guidance", "harness", "1.0",
                    prompt_text["git-read-guidance"],
                    requires_any_tools=("git.status", "git.diff", "git.log"),
                ),
                PromptTemplateSegment(
                    "source-read-guidance", "harness", "1.3",
                    prompt_text["source-read-guidance"],
                    requires_any_tools=("source.github.resolve",),
                ),
                PromptTemplateSegment(
                    "external-document-guidance", "harness", "1.0",
                    prompt_text["external-document-guidance"],
                    requires_any_tools=("web.fetch_markdown",),
                ),
                PromptTemplateSegment(
                    "live-fact-routing", "harness", "2.2",
                    prompt_text["live-fact-routing"],
                    requires_any_tools=("web.search",),
                ),
                PromptTemplateSegment(
                    "runtime-fact-routing", "harness", "1.1",
                    prompt_text["runtime-fact-routing"],
                    requires_any_tools=("core.run_command",),
                ),
                # ---- call mechanics, working memory, and plan bookkeeping:
                # these describe how to make a call or record progress, not what
                # any single capability is for ----
                PromptTemplateSegment(
                    "parallel-tool-call-guidance", "harness", "1.1",
                    prompt_text["parallel-tool-call-guidance"],
                ),
                PromptTemplateSegment(
                    "working-memory-guidance", "harness", "1.0",
                    prompt_text["working-memory-guidance"],
                    requires_any_tools=("core.working_memory_update",),
                ),
                PromptTemplateSegment(
                    "task-plan-guidance", "harness", "3.3",
                    prompt_text["task-plan-guidance"],
                    requires_any_tools=("core.todo_list",),
                ),
            ),
        )
        expected = {segment.segment_id for segment in template.static_segments}
        unexpected = prompt_text.keys() - expected
        if unexpected:
            raise ValueError(f"unknown packaged prompt sections: {sorted(unexpected)}")
        return template

    def manifest(self) -> PromptManifest:
        static = tuple(
            segment.manifest_data(index)
            for index, segment in enumerate(self.static_segments, start=1)
        )
        order = (
            *(segment.segment_id for segment in self.static_segments),
            "trusted_project_instructions_if_present",
            "task_spec_if_present",
            "runtime_instruction_if_present",
            "untrusted_project_onboarding_if_present",
            "untrusted_project_memory_if_present",
            "session_conversation_projection_if_present",
            "task_working_memory_if_present",
            "conversation_history", "current_user_input",
        )
        content = {
            "schema_version": 1, "manifest_id": self.manifest_id,
            "revision": self.revision, "assembly_order": order,
            "static_segments": static,
        }
        return PromptManifest(
            schema_version=1, manifest_id=self.manifest_id,
            revision=self.revision, assembly_order=order, static_segments=static,
            manifest_hash=canonical_hash(content),
        )

    def assemble(
        self, conversation: tuple[Message, ...], tools: tuple[ToolSpec, ...],
        runtime_instruction: str | None = None,
    ) -> PromptAssembly:
        if any(
            message.role is MessageRole.SYSTEM
            and not message.message_id.startswith((
                "project-instructions-context-", "task-spec-context-",
                "conversation-environment-context-",
            ))
            for message in conversation
        ):
            raise ValueError("conversation cannot inject system-role messages")
        visible_tool_names = frozenset(tool.name for tool in tools)
        applicable_segments = tuple(
            segment for segment in self.static_segments
            if segment.applies_to(visible_tool_names)
        )
        system_messages = tuple(
            Message(
                message_id=f"prompt-{segment.segment_id}-r{self.revision}",
                role=MessageRole.SYSTEM, content=(TextBlock(segment.content),),
            )
            for segment in applicable_segments
        )
        normalized_runtime_instruction = (runtime_instruction or "").strip()
        runtime_messages = (
            (Message(
                message_id=f"prompt-runtime-r{self.revision}",
                role=MessageRole.SYSTEM,
                content=(TextBlock(normalized_runtime_instruction),),
            ),)
            if normalized_runtime_instruction else ()
        )
        project_instruction_messages = tuple(
            message for message in conversation
            if message.message_id.startswith("project-instructions-context-")
        )
        task_spec_messages = tuple(
            message for message in conversation
            if message.message_id.startswith("task-spec-context-")
        )
        conversation_environment_messages = tuple(
            message for message in conversation
            if message.message_id.startswith("conversation-environment-context-")
        )
        onboarding_messages = tuple(
            message for message in conversation
            if message.message_id.startswith("project-onboarding-context-")
        )
        memory_messages = tuple(
            message for message in conversation
            if message.message_id.startswith("project-memory-context-")
        )
        session_messages = tuple(
            message for message in conversation
            if message.message_id.startswith("session-context-")
        )
        document_reference_messages = tuple(
            message for message in conversation
            if message.message_id.startswith("document-references-context-")
        )
        working_memory_messages = tuple(
            message for message in conversation
            if message.message_id.startswith("working-memory-context-")
        )
        ordinary_conversation = tuple(
            message for message in conversation
            if not message.message_id.startswith((
                "project-onboarding-context-", "project-memory-context-",
                "session-context-", "document-references-context-",
                "working-memory-context-", "project-instructions-context-",
                "task-spec-context-", "conversation-environment-context-",
            ))
        )
        if any(
            message.role is not MessageRole.SYSTEM
            for message in project_instruction_messages
        ):
            raise ValueError("project instructions must use the trusted system role")
        if any(message.role is not MessageRole.SYSTEM for message in task_spec_messages):
            raise ValueError("Task SPEC context must use the protected system role")
        if any(
            message.role is not MessageRole.SYSTEM
            for message in conversation_environment_messages
        ):
            raise ValueError(
                "conversation runtime environment must use the protected system role"
            )
        if any(message.role is not MessageRole.USER for message in onboarding_messages):
            raise ValueError("project onboarding context must use the untrusted user role")
        if any(message.role is not MessageRole.USER for message in memory_messages):
            raise ValueError("project memory context must use the untrusted user role")
        if any(message.role is not MessageRole.USER for message in session_messages):
            raise ValueError("Session context must use the untrusted user role")
        if any(
            message.role is not MessageRole.USER
            for message in document_reference_messages
        ):
            raise ValueError("document references must use the untrusted user role")
        if any(
            message.role is not MessageRole.USER for message in working_memory_messages
        ):
            raise ValueError("working memory context must use the untrusted user role")
        messages = (
            system_messages + project_instruction_messages + task_spec_messages
            + conversation_environment_messages
            + runtime_messages + onboarding_messages
            + memory_messages
            + tuple(row for message in session_messages
                    for row in render_session_history(message))
            + document_reference_messages + working_memory_messages
            + ordinary_conversation
        )
        manifest = self.manifest()
        tool_data = tuple(sorted((
            {
                "name": tool.name,
                "description_hash": canonical_hash(tool.description),
                "parameters_hash": canonical_hash(dict(tool.parameters)),
            } for tool in tools
        ), key=lambda item: item["name"]))
        ordered_tool_data = tuple({
            "name": tool.name,
            "description_hash": canonical_hash(tool.model_description()),
            "parameters_hash": canonical_hash(dict(tool.parameters)),
        } for tool in tools)
        message_data = tuple({
            "ordinal": index, "role": message.role.value,
            "content_hash": canonical_hash(message.to_data()["content"]),
        } for index, message in enumerate(messages, start=1))
        toolset_hash = canonical_hash(tool_data)
        effective_hash = canonical_hash({
            "manifest_hash": manifest.manifest_hash,
            "messages": message_data, "tools": tool_data,
        })
        return PromptAssembly(messages, PromptAssemblyReceipt(
            manifest_hash=manifest.manifest_hash,
            effective_prompt_hash=effective_hash, message_count=len(messages),
            static_segment_count=len(system_messages),
            conversation_message_count=len(conversation), tool_count=len(tools),
            toolset_hash=toolset_hash,
            role_order=tuple(message.role.value for message in messages),
            static_segment_ids=tuple(segment.segment_id for segment in applicable_segments),
            tool_names=tuple(item["name"] for item in tool_data),
            stable_prefix_hash=canonical_hash(tuple(
                {"role": message.role.value,
                 "content_hash": canonical_hash(message.to_data()["content"])}
                for message in system_messages
            )),
            ordered_toolset_hash=canonical_hash(ordered_tool_data),
            message_structure=tuple(_message_structure(message) for message in messages),
        ))


def _message_structure(message: Message) -> dict[str, Any]:
    """Describe request shape without storing message text or tool arguments."""
    prefixes = (
        "prompt-", "session-context-", "session-history-", "session-facts-", "project-instructions-context-",
        "task-spec-context-", "conversation-environment-context-",
        "project-onboarding-context-", "project-memory-context-",
        "document-references-context-", "working-memory-context-",
    )
    source = next((prefix[:-1] for prefix in prefixes
                   if message.message_id.startswith(prefix)), "conversation")
    item: dict[str, Any] = {
        "role": message.role.value,
        "source": source,
        "blocks": [{"TextBlock": "text", "ImageBlock": "input_image",
                    "ToolCallBlock": "tool_call", "ToolResultBlock": "tool_result",
                    "ConclusionBlock": "conclusion"}.get(type(block).__name__, "unknown")
                   for block in message.content],
        "text_characters": len(message.text),
    }
    results = [block.result for block in message.content
               if isinstance(block, ToolResultBlock)]
    if results:
        item["tool_result_count"] = len(results)
        item["tool_result_truncated_count"] = sum(result.truncated for result in results)
        item["tool_result_failed_count"] = sum(not result.ok for result in results)
    if source == "session-context":
        try:
            body = json.loads(message.text)
        except (ValueError, TypeError, RecursionError):
            body = None
        if isinstance(body, dict):
            history = body.get("history_entries") or body.get("recent_messages")
            if isinstance(history, list):
                item["history_roles"] = [
                    row.get("role", "unknown") if row.get("role") in
                    ("user", "assistant", "system", "tool") else "unknown"
                    for row in history if isinstance(row, dict)
                ]
            item["has_work_state"] = isinstance(body.get("work_state"), dict)
            summaries = body.get("recent_task_summaries")
            item["task_summary_count"] = len(summaries) if isinstance(summaries, list) else 0
    return item
