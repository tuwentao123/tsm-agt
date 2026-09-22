"""Who may author a Task goal, and what that goal must contain.

The router used to return a free-text goal alongside its classification, and
Runtime accepted it after checking only that it was non-empty. A routing model
then answered "combine the request with the selected Task summary" by copying the
source Task's goal forward and appending one clause, so the user's own message
never reached the Agent at all: the reply addressed the previous Task instead.

The classification stays with the model because every field of it is checkable
against an enum or the supplied catalog. The goal does not, because free text is
not checkable. These tests pin that split, and pin what the deterministic
construction must contain: the request as the body, the source Task only as
labelled reference.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.model_session_input.resolver import (
    SESSION_ROUTE_PROPOSAL_SCHEMA,
)
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    SessionRouteDisposition, SessionTaskRelation, TaskState,
)
from tsm_agt.ports import (
    AdapterDescriptor, HealthState, HealthStatus, ProviderCapabilities,
)
from tsm_agt.sdk import EngineeringAgentClient

FABRICATED_GOAL = (
    "基于此前已完成并验证的成果，继续修复当前运行态问题：重点检查排序逻辑、"
    "插入顺序与恢复后的默认滚动位置，并验证修改真实生效。"
)


class ScriptedResolver:
    """Returns a fixed classification, plus a goal it is no longer allowed to set."""

    descriptor = AdapterDescriptor(
        "fixture.scripted-route", "1.0", "SessionInputResolverPort", "1.0",
    )

    def __init__(self, relation: str, *, offer_goal: bool = True) -> None:
        self.relation = relation
        self.offer_goal = offer_goal
        self.source_task_id: str | None = None

    async def start(self, context): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def stop(self, deadline): pass

    async def resolve_session_input(self, _text, context):
        derived = self.relation in {"FOLLOW_UP", "BRANCH"}
        catalog = context["task_catalog"]
        source = str(catalog[0]["task_id"]) if derived and catalog else None
        self.source_task_id = source
        proposal = {
            "disposition": "CREATE_TASK",
            "relation": self.relation,
            "source_task_id": source,
            "input_grounding": (
                "CONTEXT_DEPENDENT" if derived else "SELF_CONTAINED"
            ),
            "confidence": 0.96,
            "reason_code": "scripted",
            "clarification": None,
            "candidate_task_ids": [],
        }
        if self.offer_goal:
            # A resolver that still volunteers a goal must not influence the Task
            # and must not fail the route either.
            proposal["resolved_goal"] = FABRICATED_GOAL
        return proposal


class VisionlessModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=8192)


class DerivedTaskGoalAuthorshipTest(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, root: Path, resolver):
        app = compose_fixture_application(
            model_adapter=VisionlessModel(), tool_adapters=(),
            session_input_resolver_adapter=resolver,
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client

    @staticmethod
    async def seed_source_task(client, root: Path, session_id: str) -> str:
        """Complete one Task so the catalog has a terminal FOLLOW_UP source."""
        kernel = client.application.kernel
        task = await kernel.create_task(
            "修复 trace 节点刷新后顺序错乱的问题", root, session_id=session_id,
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING, TaskState.VERIFYING,
            TaskState.FINALIZING, TaskState.SUCCEEDED,
        ):
            await kernel.transition_task(task.task_id, state, state.value)
        return task.task_id

    async def resolve(self, relation: str, text: str, *, offer_goal: bool = True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        resolver = ScriptedResolver(relation, offer_goal=offer_goal)
        client = await self.make_client(root, resolver)
        kernel = client.application.kernel
        session = await kernel.create_session(relation)
        source_task_id = await self.seed_source_task(
            client, root, session.session_id
        )
        decision = await kernel.resolve_session_input(
            session.session_id, text, root
        )
        return decision, source_task_id, text

    async def test_derived_goal_is_built_by_runtime_not_the_resolver(self) -> None:
        decision, source_task_id, text = await self.resolve(
            "FOLLOW_UP", "还是不行啊"
        )

        self.assertEqual(decision.relation, SessionTaskRelation.FOLLOW_UP)
        self.assertEqual(decision.source_task_id, source_task_id)
        assert decision.resolved_goal is not None
        self.assertNotEqual(decision.resolved_goal, FABRICATED_GOAL)
        self.assertNotIn(FABRICATED_GOAL, decision.resolved_goal)
        # The deterministic handoff, recognisable by its own markers.
        self.assertIn("[session-follow-up]", decision.resolved_goal)
        self.assertIn("Current request:", decision.resolved_goal)

    async def test_derived_goal_carries_the_request_as_its_body(self) -> None:
        """The user's words must survive; that is the whole point of the fix."""
        decision, _, text = await self.resolve(
            "FOLLOW_UP", "这一块有方法改成可视化更好的格式吗"
        )

        assert decision.resolved_goal is not None
        self.assertIn(text, decision.resolved_goal)
        request_index = decision.resolved_goal.index("Current request:")
        source_index = decision.resolved_goal.index("Authority-free source Task:")
        self.assertLess(request_index, source_index)

    async def test_source_goal_appears_only_as_labelled_reference(self) -> None:
        """Reusing the source goal as the body is the defect, not the fix."""
        decision, _, _ = await self.resolve("FOLLOW_UP", "接着弄")

        assert decision.resolved_goal is not None
        goal = decision.resolved_goal
        self.assertIn("- original goal: 修复 trace 节点刷新后顺序错乱的问题", goal)
        # The reference sits inside the source section, never before the request.
        self.assertGreater(
            goal.index("- original goal:"), goal.index("Current request:")
        )
        self.assertIn("Safety boundary:", goal)

    async def test_branch_uses_the_same_deterministic_construction(self) -> None:
        decision, source_task_id, text = await self.resolve("BRANCH", "换个思路试试")

        self.assertEqual(decision.relation, SessionTaskRelation.BRANCH)
        self.assertEqual(decision.source_task_id, source_task_id)
        assert decision.resolved_goal is not None
        self.assertIn("[session-follow-up]", decision.resolved_goal)
        self.assertIn(text, decision.resolved_goal)

    async def test_independent_goal_is_exactly_the_request(self) -> None:
        """With no source there is nothing to name, so no template applies."""
        decision, _, text = await self.resolve(
            "INDEPENDENT", "解释一下 Session 和 Task 的区别"
        )

        self.assertEqual(decision.disposition, SessionRouteDisposition.CREATE_TASK)
        self.assertEqual(decision.relation, SessionTaskRelation.INDEPENDENT)
        self.assertIsNone(decision.source_task_id)
        self.assertEqual(decision.resolved_goal, text)

    async def test_contextual_goal_is_exactly_the_request(self) -> None:
        decision, _, text = await self.resolve("CONTEXTUAL", "顺便看下这个")

        self.assertEqual(decision.relation, SessionTaskRelation.CONTEXTUAL)
        self.assertIsNone(decision.source_task_id)
        self.assertEqual(decision.resolved_goal, text)

    async def test_a_resolver_that_omits_a_goal_still_routes(self) -> None:
        """The goal is no longer part of the contract, so absence is normal."""
        decision, source_task_id, text = await self.resolve(
            "FOLLOW_UP", "继续", offer_goal=False,
        )

        self.assertEqual(decision.source_task_id, source_task_id)
        assert decision.resolved_goal is not None
        self.assertIn(text, decision.resolved_goal)

    async def test_derived_route_still_requires_a_source_task(self) -> None:
        """Dropping the goal requirement must not drop the source requirement."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class SourcelessResolver(ScriptedResolver):
                async def resolve_session_input(self, text, context):
                    proposal = await super().resolve_session_input(text, context)
                    proposal["source_task_id"] = None
                    return proposal

            client = await self.make_client(root, SourcelessResolver("FOLLOW_UP"))
            kernel = client.application.kernel
            session = await kernel.create_session("sourceless")
            await self.seed_source_task(client, root, session.session_id)

            decision = await kernel.resolve_session_input(
                session.session_id, "接着那个改", root
            )

            # The router failure degrades to a safe CONTEXTUAL Task carrying the
            # user's text, rather than inventing a derived Task without a source.
            self.assertEqual(
                decision.relation, SessionTaskRelation.CONTEXTUAL
            )
            self.assertIsNone(decision.source_task_id)
            self.assertEqual(decision.resolved_goal, "接着那个改")

    def test_the_route_contract_has_no_goal_field(self) -> None:
        """Removing the field is what makes the defect unexpressible."""
        self.assertNotIn(
            "resolved_goal", SESSION_ROUTE_PROPOSAL_SCHEMA["properties"]
        )
        self.assertNotIn("resolved_goal", SESSION_ROUTE_PROPOSAL_SCHEMA["required"])
        # Every remaining field is checkable against an enum or the catalog.
        self.assertEqual(
            set(SESSION_ROUTE_PROPOSAL_SCHEMA["required"]),
            {
                "disposition", "relation", "source_task_id", "input_grounding",
                "confidence", "reason_code", "clarification",
                "candidate_task_ids",
            },
        )


if __name__ == "__main__":
    unittest.main()
