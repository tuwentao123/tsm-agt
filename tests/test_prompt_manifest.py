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

    def test_externalized_prompt_preserves_existing_manifest(self) -> None:
        # Preserve the assembled static content, ordering, versions and gates.
        # The edit guidance intentionally changed for read/hash dependencies.
        self.assertEqual(
            self.template.manifest().manifest_hash,
            "0f471253fba04074b704dc762e0fb3292bd9fe3176620b684da0fc0daa21566b",
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
        self.assertLess(ids.index("project-instructions-context-project"), ids.index("prompt-runtime-r42"))
        self.assertLess(ids.index("task-spec-context-task"), ids.index("prompt-runtime-r42"))
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

        self.assertEqual(segment.version, "4.2")
        self.assertIn("# Working with the user\n", content)
        self.assertIn("# Evidence and verification\n", content)
        self.assertIn("analyze or review without changing files", content)
        self.assertIn("Prior conclusions can be wrong or stale", content)
        self.assertIn("A report is not proof of its cause", content)
        self.assertIn("not that the user's objective is achieved", content)
        self.assertIn("its own relevant material", content)
        self.assertIn("Use only advertised tools", content)
        self.assertIn("resolved_root", content)
        self.assertNotIn("Answer Use", content)
        self.assertNotIn("unverified parts. about", content)
        # Facts, not intent regexes: no tool may be named here, so the segment
        # stays ungated and ships to every session.
        self.assertFalse(segment.requires_any_tools)
        for capability in ("core.search_files", "web.search", "source.github"):
            self.assertNotIn(capability, content)
        self.assertNotIn("ExplicitScopeHint", content)
        self.assertNotIn("turn_intent", content)

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
        self.assertEqual(self.template.revision, 42)

    def test_workspace_guidance_is_capability_bound(self) -> None:
        search = self.segment("workspace-search-guidance").content
        read = self.segment("workspace-read-guidance").content
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

        self.assertEqual(segment.version, "4.1")
        # Completion-first posture: do the work now, do not yield with a plan,
        # keep going until done, confirm each change, never claim unverified work.
        self.assertIn("carry out the edits now", segment.content)
        self.assertIn("do not end the turn with only a plan", segment.content)
        self.assertIn("a read is preparation, not the deliverable", segment.content)
        self.assertIn(
            "keep going across steps until the work is actually done", segment.content
        )
        self.assertIn("concrete blocker", segment.content)
        self.assertIn("materially changes scope", segment.content)
        # Verify-after-edit is now a default step, not optional.
        self.assertIn("After each change, confirm it took", segment.content)
        self.assertIn("a receipt, not proof the goal is met", segment.content)
        self.assertIn(
            "never present unperformed or unverified work as done", segment.content
        )
        self.assertEqual(
            segment.requires_any_tools,
            (
                "core.apply_patch", "core.apply_patch_v4a",
                "core.apply_patches", "core.delete_file",
            ),
        )

    def test_delivery_guidance_is_capability_gated_not_turn_dynamic(self) -> None:
        without_write = self.template.assemble((self.user,), (self.tool(),))
        with_write = self.template.assemble((self.user,), (
            self.tool(), tool("core.apply_patch_v4a"),
        ))

        self.assertNotIn(
            "delivery mode", "\n".join(message.text for message in without_write.messages),
        )
        rendered = "\n".join(message.text for message in with_write.messages)
        self.assertIn("When the user asks you to implement", rendered)
        self.assertIn("carry out the edits now", rendered)

    def test_tool_feedback_guidance_is_stable_base_and_capability_free(self) -> None:
        segment = self.segment("tool-feedback-guidance")
        content = segment.content

        self.assertEqual(segment.version, "1.2")
        # Stable base layer: it ships to every session regardless of toolset.
        self.assertEqual(segment.requires_any_tools, ())
        self.assertIn("# Working from tool results\n", content)
        # Route-A multi-step persistence: finish all implied steps in the turn.
        self.assertIn("carry them all out in the same turn", content)
        # A result, including a failure, is a step fact, not whole-task success.
        self.assertIn("not as success or failure of the whole request", content)
        # Persistence: a single failure does not end the task; keep working.
        self.assertIn("does not end the task and does not forbid later work", content)
        self.assertIn("keep working the current request", content)
        # Stopping and reporting a blocker is the genuine-exception case.
        self.assertIn("no permitted way forward remains", content)
        self.assertIn("report a blocker only in that genuine case", content)
        self.assertIn("other goals or their verification are complete", content)
        # Denials are not bypassed; unknown effects are not blindly retried.
        self.assertIn("Do not bypass a user or policy denial", content)
        self.assertIn("blindly repeat a call whose effect is unknown", content)
        self.assertIn("continue the independent work you are allowed to do", content)
        # A general disposition, not a scenario patch: it names no capability
        # and enumerates no specific failure category to react to.
        for capability in (
            "core.run_command", "core.read_file", "core.apply_patch", "web.search",
        ):
            self.assertNotIn(capability, content)
        for scenario in ("expected_hash", "syntax", "argument", "ambiguous"):
            self.assertNotIn(scenario, content)

    def test_tool_feedback_guidance_ships_without_any_tools(self) -> None:
        assembled = self.template.assemble((self.user,), ())
        rendered = "\n".join(message.text for message in assembled.messages)
        self.assertIn(
            "Read each tool result, including a failure, as a fact about that step",
            rendered,
        )
        self.assertIn(
            "never present failed, unverified, or abandoned work as done", rendered,
        )

    def test_request_and_evidence_guidance_applies_without_write_tools(self) -> None:
        assembled = self.template.assemble((self.user,), ())
        rendered = "\n".join(message.text for message in assembled.messages)
        self.assertIn("analyze or review without changing files", rendered)
        self.assertIn("Prior conclusions can be wrong or stale", rendered)
        self.assertIn("not that the user's objective is achieved", rendered)
        self.assertNotIn("a workspace write was actually attempted", rendered)

    def test_assembled_request_guidance_has_complete_sentences(self) -> None:
        assembled = self.template.assemble((self.user,), ())
        message = next(m for m in assembled.messages
                       if m.message_id.startswith("prompt-request-pipeline-guidance-"))
        self.assertEqual(message.text, (
            "# Working with the user\n"
            "Understand the current request using relevant conversation history. "
            "Respect the user's scope: analyze or review without changing files; "
            "implement, run, or verify when requested and authorized. An ordinary "
            "follow-up may continue earlier work or change direction; use its context. "
            "Complete feasible in-scope work rather than offering to do it later. "
            "Ask for a decision only when a material ambiguity, authorization, or "
            "blocker prevents safe progress.\n\n"
            "During multi-step work, when useful, briefly tell the user what you learned or what you will do "
            "next in normal assistant text alongside your tool calls. Continue the work in the same response; "
            "do not substitute an update for a tool call. Describe observations and uncertainty accurately, "
            "and do not claim an action succeeded before its result. These notes are not hidden reasoning. "
            "After receiving tool results, if another tool call is needed, include a concise progress update "
            "in the same assistant message as that call: summarize what the results actually established, "
            "mention a failure or uncertainty when relevant, and say what you will check or do next. Continue "
            "with the tool call; do not claim an unverified outcome or repeat the update in the final answer.\n\n"
            "# Evidence and verification\n"
            "Distinguish user reports, tool observations, prior assistant conclusions, "
            "and hypotheses. Prior conclusions can be wrong or stale: reconsider them "
            "when corrected or contradicted. A report is not proof of its cause. "
            "A tool result establishes only what it actually reports, not that the "
            "user's objective is achieved. Select checks suited to the request, "
            "available tools, and permissions; report results and unverified parts. "
            "When a further permitted check would materially improve the answer, "
            "perform it. Support claims about a named external implementation with "
            "its own relevant material; earlier answers and these instructions are "
            "not evidence. If retrieval is blocked, explain what remains unverified. "
            "Use only advertised tools, never invent results, and respect user-named "
            "workspace paths and resolved_root."
        ))

    def test_workspace_command_guidance_keeps_cwd_in_workspace(self) -> None:
        segment = self.segment("workspace-command-guidance")

        self.assertEqual(segment.version, "1.5")
        self.assertEqual(
            segment.requires_any_tools, ("core.run_command", "core.process_wait"),
        )
        self.assertIn("always provide cwd explicitly", segment.content)
        self.assertIn("Use '.' for the task workspace root", segment.content)
        self.assertIn("workspace-relative", segment.content)
        self.assertIn("host absolute path", segment.content)
        self.assertIn("exact workspace-relative path in cwd", segment.content)
        self.assertIn("retry once with that verified child cwd", segment.content)
        self.assertIn("managed-process start fact", segment.content)
        self.assertIn("manual user instructions", segment.content)
        self.assertIn("core.process_wait", segment.content)
        # The command-specific unknown-outcome fact stays here; the generic
        # cross-tool failure/denial guidance moved to tool-feedback-guidance.
        self.assertIn("has an unknown outcome", segment.content)
        self.assertNotIn("structured recovery", segment.content)
        self.assertNotIn("never call failed work completed", segment.content.lower())
        self.assertNotIn("not independent authorized work", segment.content)

    def test_task_plan_guidance_is_capability_bound(self) -> None:
        segment = self.segment("task-plan-guidance")

        # The plan stays a model-managed execution aid rather than a Runtime
        # completion classifier.  The prompt only establishes generic planning,
        # execution, and verification discipline.
        self.assertIn("when a plan helps", segment.content)
        self.assertIn("skip it for straightforward tasks", segment.content)
        self.assertIn("failed verification does not complete", segment.content)
        self.assertIn("current request can change its scope", segment.content)
        # Lean toward completion: work the list, do not stop after one item.
        self.assertIn("until the list is complete", segment.content)
        self.assertEqual(segment.version, "3.1")

    def test_execution_discipline_keeps_looking_until_grounded(self) -> None:
        segment = self.segment("execution-discipline")

        # The reference harness keeps these in its persistence and
        # prerequisite-check rules: a
        # partial result is a reason to look again rather than conclude, and never
        # stop while another call would materially change the answer. They live in
        # this segment rather than a new one because its old wording already
        # carried the first half, and duplicating it cost more prompt budget
        # than the tiny-window fixtures have. The "done means grounded" half
        # moved to request-pipeline-guidance, which owns the closing rule.
        self.assertIn("Do not stop while another read or search", segment.content)
        self.assertIn("partial, or truncated result is not an answer", segment.content)
        self.assertIn("gather prerequisite context", segment.content)
        self.assertIn("what is unverified", segment.content)
        self.assertNotIn("backed by what you read", segment.content)
        self.assertEqual(segment.version, "1.3")

    def test_source_read_guidance_rejects_proxy_evidence(self) -> None:
        segment = self.segment("source-read-guidance")

        # This workspace's own notes about another system are not that system.
        self.assertIn("are not its", segment.content)
        self.assertIn("source.github.get_file", segment.content)
        self.assertEqual(segment.version, "1.3")

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
        self.assertEqual(live.version, "2.2")
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
        self.assertIn("source.github.get_file", content)
        self.assertIn("instead of repeating an earlier turn's blocker", content)
        self.assertNotIn("core.run_command", content)
        self.assertNotIn("core.read_file", content)

    def test_execution_discipline_is_generic_and_capability_free(self) -> None:
        segment = self.segment("execution-discipline")
        content = segment.content

        self.assertIn("not an answer", content)
        self.assertIn("change ", content)
        self.assertIn("never repeat an unchanged failing call", content)
        self.assertIn("what is unverified", content)
        # Still capability-free: it names no tool, so it ships to every session.
        self.assertEqual(segment.version, "1.3")
        self.assertEqual(segment.requires_any_tools, ())
        for unavailable_capability in (
            "browser", "curl", "git clone", "web.search", "core.run_command",
        ):
            self.assertNotIn(unavailable_capability, content)

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
        content = segment.content
        self.assertEqual(segment.version, "3.0")
        self.assertIn("stable general concept", content)
        self.assertIn("each subject before comparing", content)
        self.assertIn("perform the authorized implementation", content)
        self.assertIn("materially change scope", content)
        self.assertIn("not evidence about any real project", content)
        self.assertFalse(segment.requires_any_tools)
        self.assertEqual(segment.version, "3.0")

    @staticmethod
    def _segment_ids(messages: tuple[Message, ...]) -> list[str]:
        return [
            message.message_id.split("prompt-", 1)[1].rsplit("-r", 1)[0]
            for message in messages
            if message.message_id.startswith("prompt-")
        ]


if __name__ == "__main__":
    unittest.main()
