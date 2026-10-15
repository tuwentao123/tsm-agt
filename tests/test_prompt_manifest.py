from __future__ import annotations

import unittest

from tsm_agt.core import PromptTemplate, PromptTemplateSegment
from tsm_agt.ports import (
    Message, MessageRole, TextBlock, ToolIdempotency, ToolResultAuthority,
    ToolRisk, ToolSpec,
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
    "system-safety", "request-pipeline-guidance",
    # Layer 2: request handling -- understand, shape, route, close.
    "response-format-guidance", "execution-discipline",
    "completion-behavior-examples",
    # Layer 3: one segment per capability, ordered workspace -> source ->
    # documents -> live facts -> runtime facts.
    "workspace-search-guidance",
    "workspace-read-guidance", "code-edit-guidance", "implementation-mode-guidance",
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
        self.assertEqual(
            gated["completion-behavior-examples"],
            ["source.github.resolve", "source.github.get_file"],
        )
        self.assertEqual(
            gated["implementation-mode-guidance"],
            ["core.apply_patch_v4a", "core.apply_patches", "core.delete_file"],
        )
        self.assertEqual(gated["workspace-command-guidance"], ["core.run_command"])
        self.assertEqual(gated["task-plan-guidance"], ["core.todo_list"])

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
                MessageRole.SYSTEM, MessageRole.SYSTEM,
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
        self.assertLess(ids.index("project-instructions-context-project"), ids.index("prompt-runtime-r38"))
        self.assertLess(ids.index("task-spec-context-task"), ids.index("prompt-runtime-r38"))
        self.assertEqual(assembled.receipt.static_segment_count, 5)

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
            data["prompt_role_order"],
            [
                "system", "system", "system", "system", "system", "user",
            ],
        )
        self.assertNotIn("inspect the project", str(data))
        self.assertNotIn("Read a workspace file", str(data))

    def test_request_pipeline_orders_understanding_before_tools(self) -> None:
        segment = self.segment("request-pipeline-guidance")
        content = segment.content

        # The pipeline is the upstream contract: settle what the request needs,
        # then shape it, and only answer from that subject's own material. It is
        # phrased as obligations for this request, never as a description of how
        # the agent works -- a turn that asks "how does an agent route tools?"
        # once got this segment paraphrased back as its answer.
        self.assertIn("settle what this request needs", content)
        self.assertIn("Name its subject", content)
        self.assertIn("if one step settles it", content)
        self.assertIn("several ordered steps", content)
        self.assertIn("only from that system's own material", content)
        self.assertIn("is not a stable general-concept question", content)
        self.assertIn("does not replace an available in-scope lookup", content)
        self.assertIn("your own earlier answers here", content)
        self.assertIn("these instructions are not evidence", content)
        self.assertIn("separate obligations", content)
        self.assertIn("After each tool result", content)
        self.assertIn("only when no material obligation remains", content)
        self.assertLess(
            content.index("settle what this request needs"),
            content.index("only from that system's own material"),
        )
        # Identity facts the old harness-identity segment owned moved with it.
        self.assertIn("honor user-named paths", content)
        self.assertIn("trust resolved_root over assumptions", content)
        self.assertIn("Use only the tools advertised in this turn", content)
        # ... including the completion rule the old segment carried.
        self.assertIn("rather than offering to continue later", content)
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
        self.assertEqual(segment.version, "1.3")
        self.assertIn("Completed (what changed", segment.content)
        self.assertIn("Verification (the command and outcome", segment.content)
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
        self.assertEqual(self.template.revision, 38)

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

    def test_implementation_guidance_is_static_and_requires_a_write_tool(self) -> None:
        segment = self.segment("implementation-mode-guidance")

        self.assertEqual(segment.version, "1.0")
        self.assertIn("treat the request as execution", segment.content)
        self.assertIn("closest available project verification", segment.content)
        self.assertEqual(
            segment.requires_any_tools,
            ("core.apply_patch_v4a", "core.apply_patches", "core.delete_file"),
        )

    def test_workspace_command_guidance_keeps_cwd_in_workspace(self) -> None:
        segment = self.segment("workspace-command-guidance")

        self.assertEqual(segment.version, "1.2")
        self.assertEqual(segment.requires_any_tools, ("core.run_command",))
        self.assertIn("always provide cwd explicitly", segment.content)
        self.assertIn("Use '.' for the task workspace root", segment.content)
        self.assertIn("workspace-relative", segment.content)
        self.assertIn("host absolute path", segment.content)
        self.assertIn("structured recovery", segment.content)
        self.assertIn("never call failed work completed", segment.content)
        self.assertIn("exact workspace-relative path in cwd", segment.content)
        self.assertIn("retry once with that verified child cwd", segment.content)
        self.assertIn("Use background mode for dev, serve, start, and watch", segment.content)
        self.assertIn("manual user instructions", segment.content)
        self.assertIn("after reading apps/web/package.json", segment.content)
        self.assertIn("cwd='apps/web', mode='background'", segment.content)

    def test_task_plan_guidance_is_capability_bound(self) -> None:
        segment = self.segment("task-plan-guidance")

        # The tool's own description already covers step bookkeeping; this rule
        # adds what was missing: one item per subject, and no closing an item on
        # the other subject's evidence.
        self.assertIn("one plan item per subject", segment.content)
        self.assertIn("that system's own", segment.content)
        self.assertIn("is still open", segment.content)
        self.assertEqual(segment.version, "1.0")

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

        self.assertIn("Do not answer from memory", runtime.content)
        self.assertIn("core.run_command", runtime.content)
        self.assertIn("git history", runtime.content)
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

        self.assertIn("stable general concept that names no external implementation", content)
        self.assertIn("repositories A and B", content)
        self.assertIn("separate obligations", content)
        self.assertIn("only then compare", content)
        self.assertIn("normalized repository root", content)
        self.assertIn("offer source reading as future work", content)
        self.assertIn("Correct first action: locate the official repository", content)
        self.assertIn("next action: read the implementation files", content)
        self.assertIn("only a lead, not implementation evidence", content)
        self.assertIn("generic agent architecture", content)
        self.assertIn("without verification", content)
        self.assertIn("ask one concise clarification", content)
        self.assertIn("not evidence about any real project", content)
        self.assertEqual(
            segment.requires_any_tools,
            ("source.github.resolve", "source.github.get_file"),
        )
        self.assertEqual(segment.version, "1.3")

    @staticmethod
    def _segment_ids(messages: tuple[Message, ...]) -> list[str]:
        return [
            message.message_id.split("prompt-", 1)[1].rsplit("-r", 1)[0]
            for message in messages
            if message.message_id.startswith("prompt-")
        ]


if __name__ == "__main__":
    unittest.main()
