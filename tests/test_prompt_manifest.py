from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tsm_agt.core import PromptTemplate, PromptTemplateSegment
from tsm_agt.ports import (
    Message, MessageRole, TextBlock, ToolIdempotency, ToolResultAuthority,
    ToolResult, ToolResultBlock, ToolRisk, ToolSpec,
)


def tool(name: str, schema_type: str = "string") -> ToolSpec:
    return ToolSpec(
        name, f"Tool {name}",
        {
            "type": "object",
            "properties": {"path": {"type": schema_type}},
            "required": ["path"],
            "additionalProperties": False,
        },
        ToolRisk.R0, True, True, ToolIdempotency.IDEMPOTENT,
        # An authority-bearing fixture tool keeps prompt capability filtering
        # independent from the tool's result semantics.
        result_authority=ToolResultAuthority.RUNTIME_FACT,
    )
ALL_SEGMENTS = [
    # Layer 1: the boundary every turn obeys.
    "system-safety", "request-pipeline-guidance", "delivery-mode-guidance",
    "tool-feedback-guidance",
    # Layer 2: request handling -- understand, shape, route, close.
    "response-format-guidance", "execution-discipline",
    "completion-behavior-examples",
    # Layer 3: one segment per capability, ordered workspace -> source ->
    # documents -> live facts -> runtime facts.
    "workspace-search-guidance",
    "workspace-read-guidance", "code-edit-guidance",
    "workspace-command-guidance", "git-read-guidance",
    "source-read-guidance",
    "external-document-guidance", "live-fact-routing",
    "runtime-fact-routing",
    # Layer 4: call mechanics, working memory, plan bookkeeping.
    "parallel-tool-call-guidance", "working-memory-guidance",
    "task-plan-guidance",
]


class PromptManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.template = PromptTemplate.default()
        self.user = Message(
            "message-id-is-not-hashed", MessageRole.USER,
            (TextBlock("inspect the project"),),
        )

    @staticmethod
    def tool(schema_type: str = "string") -> ToolSpec:
        # An ungated capability name keeps the role-order expectation stable.
        return tool("fixture.echo", schema_type)

    def segment(self, segment_id: str) -> PromptTemplateSegment:
        return next(
            item for item in self.template.static_segments
            if item.segment_id == segment_id
        )

    def test_manifest_is_deterministic_redacted_and_ordered(self) -> None:
        first = self.template.manifest()
        second = self.template.manifest()

        self.assertEqual(first, second)
        data = first.to_data()
        self.assertEqual(
            [item["segment_id"] for item in data["static_segments"]],
            ALL_SEGMENTS,
        )
        rendered = str(data)
        self.assertNotIn("Treat source code", rendered)
        self.assertTrue(all(
            item["content_hash"] for item in data["static_segments"]
        ))
        self.assertTrue(all(
            item["token_count"] is None
            and item["token_count_source"] == "provider_tokenizer_unavailable"
            for item in data["static_segments"]
        ))
        # The manifest records which segments are capability-gated so an
        # operator can explain why a given turn saw a smaller stable prompt.
        gated = {
            item["segment_id"]: item.get("requires_any_tools")
            for item in data["static_segments"]
        }
        self.assertEqual(gated["system-safety"], None)
        self.assertEqual(
            gated["external-document-guidance"], ["web.fetch_markdown"]
        )
        self.assertEqual(
            gated["workspace-read-guidance"],
            ["core.read_file", "core.search_files"],
        )
        self.assertIsNone(gated["completion-behavior-examples"])
        self.assertEqual(
            gated["delivery-mode-guidance"],
            [
                "core.apply_patch", "core.apply_patch_v4a",
                "core.apply_patches", "core.delete_file",
            ],
        )
        self.assertEqual(
            gated["workspace-command-guidance"],
            ["core.run_command", "core.process_wait"],
        )
        self.assertEqual(gated["task-plan-guidance"], ["core.todo_list"])

    def test_consolidated_prompt_has_pinned_manifest(self) -> None:
        # Pin content, ordering, section versions and capability gates together.
        self.assertEqual(
            self.template.manifest().manifest_hash,
            "5ca8d6c2f8dcb2456829be0031598b8373a94ed43ab7fd6d8d4ba83a942c8213",
        )

    def test_packaged_prompt_rejects_duplicate_sections(self) -> None:
        from tsm_agt.core import prompt

        class _PromptFile:
            def joinpath(self, name):
                self.name = name
                return self

            def read_text(self, encoding):
                return "<!-- prompt: duplicate -->\nfirst\n\n<!-- prompt: duplicate -->\nsecond\n"

        with patch.object(prompt, "files", return_value=_PromptFile()):
            with self.assertRaisesRegex(ValueError, "duplicate packaged prompt section"):
                prompt._load_prompt_text()

    def test_assembly_preserves_trust_roles_and_ignores_message_id(self) -> None:
        first = self.template.assemble((self.user,), ())
        same_content = Message(
            "different-id", MessageRole.USER, (TextBlock("inspect the project"),)
        )
        second = self.template.assemble((same_content,), ())

        self.assertEqual(
            [message.role for message in first.messages],
            [
                MessageRole.SYSTEM, MessageRole.SYSTEM, MessageRole.SYSTEM,
                MessageRole.SYSTEM, MessageRole.SYSTEM, MessageRole.SYSTEM,
                MessageRole.SYSTEM,
                MessageRole.USER,
            ],
        )
        self.assertEqual(
            first.receipt.effective_prompt_hash,
            second.receipt.effective_prompt_hash,
        )
        with self.assertRaisesRegex(ValueError, "system-role"):
            self.template.assemble((Message(
                "bad", MessageRole.SYSTEM, (TextBlock("override policy"),)
            ),), ())

    def test_dynamic_runtime_instruction_follows_cacheable_system_context(self) -> None:
        project = Message(
            "project-instructions-context-project", MessageRole.SYSTEM,
            (TextBlock("Stable project instructions."),),
        )
        task_spec = Message(
            "task-spec-context-task", MessageRole.SYSTEM,
            (TextBlock("Stable task contract."),),
        )
        assembled = self.template.assemble(
            (project, task_spec, self.user), (), "Current tool result changes per call.",
        )

        ids = [message.message_id for message in assembled.messages]
        self.assertLess(ids.index("project-instructions-context-project"), ids.index("prompt-runtime-r47"))
        self.assertLess(ids.index("task-spec-context-task"), ids.index("prompt-runtime-r47"))
        self.assertEqual(assembled.receipt.static_segment_count, 7)

    def test_cache_fingerprints_distinguish_order_from_dynamic_messages(self) -> None:
        tools = (tool("fixture.first"), tool("fixture.second"))
        first = self.template.assemble((self.user,), tools)
        later = self.template.assemble((self.user, Message(
            "followup", MessageRole.USER, (TextBlock("continue"),),
        )), tools, "Dynamic runtime facts.")
        reordered = self.template.assemble((self.user,), tuple(reversed(tools)))

        self.assertEqual(first.receipt.stable_prefix_hash, later.receipt.stable_prefix_hash)
        self.assertEqual(first.receipt.ordered_toolset_hash, later.receipt.ordered_toolset_hash)
        # The legacy toolset hash represents a set, not the actual order sent.
        self.assertEqual(first.receipt.toolset_hash, reordered.receipt.toolset_hash)
        self.assertNotEqual(first.receipt.ordered_toolset_hash, reordered.receipt.ordered_toolset_hash)
        self.assertNotEqual(first.receipt.effective_prompt_hash, later.receipt.effective_prompt_hash)
        receipt = later.receipt.event_data()
        self.assertNotIn("Dynamic runtime facts", str(receipt))
        self.assertNotIn("continue", str(receipt))

    def test_adding_conversation_preserves_existing_static_messages(self) -> None:
        tools = (tool("core.read_file"),)
        first = self.template.assemble((self.user,), tools)
        later = self.template.assemble((self.user, Message(
            "followup", MessageRole.USER, (TextBlock("continue"),),
        )), tools)
        self.assertEqual(first.messages, later.messages[:len(first.messages)])

    def test_content_template_and_tool_schema_change_effective_hash(self) -> None:
        original = self.template.assemble((self.user,), (self.tool(),))
        changed_user = self.template.assemble((Message(
            "user-2", MessageRole.USER, (TextBlock("inspect another project"),)
        ),), (self.tool(),))
        changed_tool = self.template.assemble(
            (self.user,), (self.tool("integer"),)
        )
        changed_template = PromptTemplate(
            "builtin.engineering-agent", 2,
            self.template.static_segments[:-1] + (PromptTemplateSegment(
                "request-pipeline-guidance", "harness", "9.9", "Changed rules."
            ),),
        ).assemble((self.user,), (self.tool(),))

        hashes = {
            original.receipt.effective_prompt_hash,
            changed_user.receipt.effective_prompt_hash,
            changed_tool.receipt.effective_prompt_hash,
            changed_template.receipt.effective_prompt_hash,
        }
        self.assertEqual(len(hashes), 4)

    def test_event_receipt_contains_hashes_not_prompt_text(self) -> None:
        receipt = self.template.assemble((self.user,), (self.tool(),)).receipt
        data = receipt.event_data()

        self.assertIn("effective_prompt_hash", data)
        self.assertEqual(
            data["prompt_static_segment_ids"],
            [segment.segment_id for segment in self.template.static_segments
             if segment.applies_to(frozenset({self.tool().name}))],
        )
        self.assertEqual(data["prompt_tool_names"], [self.tool().name])
        self.assertEqual(
            data["prompt_role_order"],
            [
                "system", "system", "system", "system", "system", "system",
                "system", "user",
            ],
        )
        self.assertNotIn("inspect the project", str(data))
        self.assertNotIn("Read a workspace file", str(data))

    def test_structural_receipt_is_redacted_and_does_not_change_prompt(self) -> None:
        history = Message(
            "session-context-test", MessageRole.USER, (TextBlock(json.dumps({
                "work_state": {"goal": "private old goal"},
                "recent_task_summaries": [{"goal": "private summary"}],
                "recent_messages": [
                    {"role": "user", "text": "private user message"},
                    {"role": "assistant", "text": "private assistant claim"},
                ],
            })),),
        )
        assembled = self.template.assemble((history, self.user), (self.tool(),))
        data = assembled.receipt.event_data()
        self.assertEqual(data["prompt_message_structure_stage"],
                         "provider_neutral_after_compaction")
        structure = data["prompt_message_structure"]
        context = next(row for row in structure if row["source"] == "session-context")
        self.assertEqual(context["history_roles"], ["user", "assistant"])
        self.assertTrue(context["has_work_state"])
        self.assertEqual(context["task_summary_count"], 1)
        self.assertNotIn("private", str(data))
        self.assertEqual(assembled.messages[-2:], (history, self.user))

    def test_structural_receipt_tolerates_invalid_session_json(self) -> None:
        history = Message("session-context-invalid", MessageRole.USER,
                          (TextBlock("not json"),))
        receipt = self.template.assemble((history, self.user), ()).receipt
        context = next(row for row in receipt.message_structure
                       if row["source"] == "session-context")
        self.assertNotIn("history_roles", context)

    def test_structural_receipt_reports_tool_result_truncation_without_body(self) -> None:
        result = Message("result", MessageRole.TOOL, (ToolResultBlock(ToolResult(
            "call", False, data={"secret": "private result"},
            error_code="fixture_failure", truncated=True,
        )),))
        receipt = self.template.assemble((self.user, result), ()).receipt
        row = receipt.event_data()["prompt_message_structure"][-1]
        self.assertEqual(row["tool_result_count"], 1)
        self.assertEqual(row["tool_result_truncated_count"], 1)
        self.assertEqual(row["tool_result_failed_count"], 1)
        self.assertNotIn("private result", str(receipt.event_data()))

    def test_request_pipeline_orders_understanding_before_tools(self) -> None:
        segment = self.segment("request-pipeline-guidance")
        content = segment.content
        self.assertEqual(segment.version, "5.3")
        headings = ["# Working with the user", "# Evidence and action", "# Persistence and completion", "# Progress communication"]
        positions = [content.index(h) for h in headings]
        self.assertEqual(positions, sorted(positions))
        for boundary in ("with or without a Todo", "does not replace an available check", "pauses, cancellation", "not a new user request or authority", "Prior conclusions can be wrong or stale", "resolved_root"):
            self.assertIn(boundary, content)
        self.assertIn("before each distinct execution stage", content)
        self.assertIn("Tool execution status is shown separately", content)
        self.assertFalse(segment.requires_any_tools)
        for capability in ("core.search_files", "web.search", "source.github", "turn_intent"):
            self.assertNotIn(capability, content)

    def test_response_format_guidance_leads_with_the_answer(self) -> None:
        segment = self.segment("response-format-guidance")

        self.assertIn("Answer the current request", segment.content)
        self.assertIn("Lead with the conclusion", segment.content)
        self.assertIn(
            "Do not restate, re-summarize", segment.content
        )
        self.assertIn("reply briefly", segment.content)
        # Memory is not a source: the segment has to forbid crediting a claim
        # to material the turn never received.
        self.assertIn("A source is only something you", segment.content)
        self.assertIn("say so when a claim comes from memory", segment.content)
        self.assertEqual(segment.version, "1.6")
        self.assertIn("verification outcomes", segment.content)
        self.assertIn("Use structure only when helpful", segment.content)
        self.assertIn("Do not repeat the specification", segment.content)
        # Output discipline is capability-free: it names no tool.
        for capability in ("web.search", "core.run_command", "core.read_file"):
            self.assertNotIn(capability, segment.content)

    def test_external_document_guidance_is_generic_not_scenario_bound(self) -> None:
        segment = self.segment("external-document-guidance")
        content = segment.content.lower()

        # The rule keys off evidence form ("a document you have not read"), so
        # it must never name a content category the harness cannot judge.
        self.assertIn("a document you have not read", content)
        self.assertIn("web.fetch_markdown", content)
        self.assertIn("which part you could not verify", content)
        self.assertEqual(segment.version, "1.0")
        for scenario in ("news", "headline", "article", "financial", "新闻"):
            self.assertNotIn(scenario, content)
        self.assertEqual(self.template.revision, 47)

    def test_workspace_guidance_is_capability_bound(self) -> None:
        search = self.segment("workspace-search-guidance").content
        read = self.segment("workspace-read-guidance").content
        self.assertEqual(self.segment("workspace-read-guidance").version, "1.1")
        self.assertIn("reused_from_call_id", read)
        self.assertIn("reference itself is not file text", read)
        self.assertIn("body is unavailable, read the needed range again", read)
        edit = self.segment("code-edit-guidance").content

        self.assertIn("core.search_files with target='content'", search)
        self.assertIn("a file-name match proves only", search)
        # Cross-project routing now lives in request-pipeline-guidance, so this
        # segment keeps only the negative warning at the tool's point of use.
        self.assertIn("material this workspace does not hold", search)
        # A named subject is content, and its docs may sit outside the obvious
        # source directory (a design-notes folder outside it was missed once).
        # This project's own names are workspace content; another project's
        # are not, so a named external system cannot be answered from here.
        self.assertIn("this project owns or mentions", search)
        self.assertIn("is not workspace content", search)
        self.assertIn("before restricting to one subtree", search)
        # A cap is a cue to continue, not a licence to conclude.
        self.assertIn("a truncated result means more exists", search)
        self.assertEqual(self.segment("workspace-search-guidance").version, "2.1")
        self.assertIn("only a lead", read)
        self.assertIn("relevant caller", read)
        self.assertIn("say what is unverified", read)
        self.assertIn("core.apply_patch_v4a", edit)
        self.assertIn("literal \\n", edit)
        self.assertIn("path, content, and hash from the same current read", edit)
        self.assertIn("do not issue that read and its dependent edit", edit)
        self.assertIn("whether the change is still needed", edit)
        self.assertIn("Report only committed edits as changes made in", edit)

    def test_delivery_guidance_is_static_and_requires_a_write_tool(self) -> None:
        segment = self.segment("delivery-mode-guidance")
        self.assertEqual(segment.version, "5.0")
        for boundary in ("carry out the edits now", "interface wiring", "receipt is not proof", "smallest sufficient verification", "after the last relevant edit", "not mandatory runtime tests", "do not repeat passing checks", "independent compilation", "never present unperformed or unverified work as done"):
            self.assertIn(boundary, segment.content)
        self.assertEqual(segment.requires_any_tools, ("core.apply_patch", "core.apply_patch_v4a", "core.apply_patches", "core.delete_file"))

    def test_delivery_guidance_is_capability_gated_not_turn_dynamic(self) -> None:
        without = self.template.assemble((self.user,), (self.tool(),))
        with_write = self.template.assemble((self.user,), (self.tool(), tool("core.apply_patch_v4a")))
        prefix = "prompt-delivery-mode-guidance-"
        self.assertFalse(any(m.message_id.startswith(prefix) for m in without.messages))
        self.assertTrue(any(m.message_id.startswith(prefix) for m in with_write.messages))

    def test_tool_feedback_guidance_is_stable_base_and_capability_free(self) -> None:
        segment = self.segment("tool-feedback-guidance")
        self.assertEqual(segment.version, "2.0")
        self.assertFalse(segment.requires_any_tools)
        for boundary in ("partial or truncated", "never repeat an unchanged failing call", "Do not bypass a user or policy denial", "unknown effects inspect state", "running process", "independent authorized", "zero exit code proves only"):
            self.assertIn(boundary, segment.content)
        for capability in ("core.run_command", "core.read_file", "web.search", "expected_hash"):
            self.assertNotIn(capability, segment.content)

    def test_tool_feedback_guidance_ships_without_any_tools(self) -> None:
        assembled = self.template.assemble((self.user,), ())
        self.assertTrue(any(m.message_id.startswith("prompt-tool-feedback-guidance-") for m in assembled.messages))

    def test_request_and_evidence_guidance_applies_without_write_tools(self) -> None:
        rendered = "\n".join(m.text for m in self.template.assemble((self.user,), ()).messages)
        self.assertIn("analyze or review without changing files", rendered)
        self.assertIn("Prior conclusions can be wrong or stale", rendered)
        self.assertIn("Answer directly when reliable information suffices", rendered)
        self.assertIn("Choose the nearest adequate evidence source", rendered)
        self.assertIn("Public discovery is for a fact", rendered)
        self.assertNotIn("# Engineering implementation", rendered)

    def test_assembled_request_guidance_has_complete_sentences(self) -> None:
        assembled = self.template.assemble((self.user,), ())
        message = next(m for m in assembled.messages if m.message_id.startswith("prompt-request-pipeline-guidance-"))
        self.assertEqual(message.text, self.segment("request-pipeline-guidance").content)
        self.assertNotIn("<!-- prompt:", message.text)
        self.assertIn("\n\n# Persistence and completion\n", message.text)

    def test_workspace_command_guidance_keeps_cwd_in_workspace(self) -> None:
        segment = self.segment("workspace-command-guidance")

        self.assertEqual(segment.version, "1.8")
        self.assertEqual(
            segment.requires_any_tools, ("core.run_command", "core.process_wait"),
        )
        self.assertIn("always provide cwd explicitly", segment.content)
        self.assertIn("Use '.' for the task workspace root", segment.content)
        self.assertIn("workspace-relative", segment.content)
        self.assertIn("host absolute path", segment.content)
        self.assertIn("exact workspace-relative path in cwd", segment.content)
        self.assertIn("retry once with that verified child cwd", segment.content)
        self.assertIn("defaults to foreground mode", segment.content)
        self.assertIn("mode='background'", segment.content)
        self.assertIn("succeeded=true", segment.content)
        self.assertIn("manual user instructions", segment.content)
        self.assertIn("core.process_wait", segment.content)
        self.assertIn("exit code proves failure but not its cause", segment.content)
        self.assertIn("core.process_logs", segment.content)
        # Terminal command output stays on this same result; generic
        # cross-tool failure/denial guidance remains in tool-feedback-guidance.
        self.assertIn("stdout/stderr", segment.content)
        self.assertIn("output was not observed", segment.content)
        self.assertIn("does not make an available general executor unavailable", segment.content)
        self.assertIn("do not infer any of those outcomes before invoking it", segment.content)
        self.assertNotIn("structured recovery", segment.content)
        self.assertNotIn("never call failed work completed", segment.content.lower())
        self.assertNotIn("not independent authorized work", segment.content)

    def test_git_guidance_distinguishes_read_tools_from_mutation_route(self) -> None:
        segment = self.segment("git-read-guidance")

        self.assertEqual(segment.version, "1.1")
        self.assertIn("Their presence does not imply that repository mutations are", segment.content)
        self.assertIn("use core.run_command with direct argv", segment.content)
        self.assertIn("returned approval, policy outcome, or command observation", segment.content)

    def test_general_executor_route_is_sent_with_git_read_tools(self) -> None:
        assembled = self.template.assemble(
            (self.user,), (tool("git.status"), tool("core.run_command")),
        )
        rendered = "\n".join(message.text for message in assembled.messages)

        self.assertIn("does not make an available general executor unavailable", rendered)
        self.assertIn("Their presence does not imply that repository mutations are", rendered)

    def test_task_plan_guidance_is_capability_bound(self) -> None:
        segment = self.segment("task-plan-guidance")
        self.assertEqual(segment.version, "4.2")
        self.assertEqual(segment.requires_any_tools, ("core.todo_list",))
        for boundary in ("skip creating a new plan for straightforward tasks", "Maintain an existing relevant plan", "latest revision", "at most one item in_progress", "verification does not complete", "Never mark unfinished work completed", "not an independent instruction", "Read only when state is missing", "do not postpone progress updates", "Example — progress", "Example — resume", "Example — scope"):
            self.assertIn(boundary, segment.content)
        # Execution persistence belongs to the generic request rules, not Todo.
        self.assertNotIn("until the list is complete", segment.content)

    def test_execution_discipline_keeps_looking_until_grounded(self) -> None:
        self.assertIn("smallest relevant observation", self.segment("execution-discipline").content)
        self.assertIn("partial or truncated", self.segment("tool-feedback-guidance").content)
        self.assertIn("gather the prerequisite evidence", self.segment("request-pipeline-guidance").content)

    def test_source_read_guidance_rejects_proxy_evidence(self) -> None:
        segment = self.segment("source-read-guidance")

        # This workspace's own notes about another system are not that system.
        self.assertIn("are not its", segment.content)
        self.assertIn("source.github.get_file", segment.content)
        self.assertEqual(segment.version, "1.4")

    def test_live_and_runtime_fact_routing_split_by_capability(self) -> None:
        live = self.segment("live-fact-routing")
        runtime = self.segment("runtime-fact-routing")

        self.assertIn("change over time", live.content)
        self.assertIn("web.search", live.content)
        self.assertIn("Never guess a repository path", live.content)
        # A search hit is a pointer; it is never treated as source content.
        self.assertIn("pointer, not content", live.content)
        self.assertIn("snippet", live.content)
        # "How does X implement Y" is an implementation claim, not knowledge.
        self.assertIn("even when it sounds conceptual", live.content)
        self.assertIn("say the answer is unverified", live.content)
        # An ambiguous name is not an invitation to describe a namesake.
        self.assertIn("not licence to describe a", live.content)
        self.assertIn("which one is meant", live.content)
        self.assertIn("Do not use public discovery as a first step", live.content)
        self.assertIn("implementation, validation, diagnosis, and recovery", live.content)
        self.assertEqual(live.version, "2.3")
        self.assertNotIn("core.run_command", live.content)

        self.assertIn("instead of guessing from memory", runtime.content)
        self.assertIn("core.run_command", runtime.content)
        self.assertIn("does not guarantee command permission", runtime.content)
        self.assertNotIn("web.search", runtime.content)

    def test_source_read_guidance_requires_reading_the_file(self) -> None:
        segment = self.segment("source-read-guidance")
        content = segment.content

        self.assertEqual(segment.requires_any_tools, ("source.github.resolve",))
        self.assertIn("reads no file", content)
        self.assertIn("proves only that a path exists", content)
        self.assertIn("default-branch index", content)
        self.assertIn("not a no-match", content)
        self.assertIn("source.github.get_file", content)
        self.assertIn("instead of repeating an earlier turn's blocker", content)
        self.assertNotIn("core.run_command", content)
        self.assertNotIn("core.read_file", content)

    def test_execution_discipline_is_generic_and_capability_free(self) -> None:
        segment = self.segment("execution-discipline")
        self.assertEqual(segment.version, "2.0")
        self.assertFalse(segment.requires_any_tools)
        self.assertIn("reuse valid evidence", segment.content)
        for capability in ("core.run_command", "web.search", "core.todo_list"):
            self.assertNotIn(capability, segment.content)

    def test_capability_gating_hides_unadvertised_tools(self) -> None:
        internal_only = self.template.assemble((self.user,), (
            tool("core.working_memory_update"), tool("core.task_outcome_complete"),
        ))
        rendered = "\n".join(message.text for message in internal_only.messages)
        for absent in (
            "web.search", "web.fetch_markdown", "core.run_command",
            "core.apply_patch_v4a", "source.github.get_file",
        ):
            self.assertNotIn(absent, rendered)
        self.assertIn("working-memory tools are advertised", rendered)

        full = self.template.assemble((self.user,), (
            tool("core.read_file"), tool("core.search_files"),
            tool("core.run_command"), tool("core.apply_patch_v4a"),
            tool("web.search"), tool("web.fetch_markdown"),
            tool("source.github.resolve"),
        ))
        full_rendered = "\n".join(message.text for message in full.messages)
        for present in (
            "web.search", "web.fetch_markdown", "core.run_command",
            "core.apply_patch_v4a", "source.github.get_file",
        ):
            self.assertIn(present, full_rendered)
        # A capability-bound prompt is never larger than the full catalog.
        self.assertLessEqual(len(full.messages), len(self.template.static_segments))

    def test_completion_examples_teach_direct_answers_and_multistep_work(self) -> None:
        segment = self.segment("completion-behavior-examples")
        self.assertEqual(segment.version, "4.0")
        self.assertFalse(segment.requires_any_tools)
        for kind in ("direct answer", "analysis", "implementation", "recovery"):
            self.assertIn("Example — " + kind + ":", segment.content)
        self.assertIn("not evidence about any real project", segment.content)
        self.assertIn("no Todo is required", segment.content)
        self.assertIn("make no changes", segment.content)
        self.assertNotIn("MCP", segment.content)

    @staticmethod
    def _segment_ids(messages: tuple[Message, ...]) -> list[str]:
        return [
            message.message_id.split("prompt-", 1)[1].rsplit("-r", 1)[0]
            for message in messages
            if message.message_id.startswith("prompt-")
        ]


if __name__ == "__main__":
    unittest.main()
