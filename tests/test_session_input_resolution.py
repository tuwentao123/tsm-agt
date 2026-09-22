from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.model_session_input import ModelSessionInputResolver
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import (
    DeterministicSessionChoiceResolver, SessionChoiceAction,
    SessionChoiceOption, SessionInputAction, SessionInteractionKind,
    SessionInteractionRequest, SessionResumeCandidate, SessionResumeSafety,
    SessionRouteDisposition, SessionTaskRelation, TaskState,
)
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, HealthState, HealthStatus, Message,
    MessageRole, ModelResponse, ModelUsage, ProviderCapabilities, TextBlock,
    ToolCall, ToolCallBlock,
)


class FixtureResolver:
    descriptor = AdapterDescriptor(
        "fixture.generic-session-resolver", "1.0",
        "SessionInputResolverPort", "1.0",
    )

    def __init__(self, response):
        self.response = response
        self.inputs = []

    async def start(self, context): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def stop(self, deadline): pass

    async def resolve_session_input(self, text, context):
        self.inputs.append((text, context))
        return dict(self.response)


class JsonModel(EchoModelProvider):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            Message(
                "resolver-result", MessageRole.ASSISTANT,
                (TextBlock(json.dumps(self.data)),),
            ), FinishReason.STOP, ModelUsage(1, 1),
        )


class RouteToolModel(EchoModelProvider):
    capabilities = ProviderCapabilities(tools=True, context_window=4096)

    def __init__(self, data):
        super().__init__()
        self.data = data
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            Message(
                "resolver-tool-result", MessageRole.ASSISTANT,
                (ToolCallBlock(ToolCall(
                    "route-proposal", "session.submit_route_proposal",
                    self.data,
                )),),
            ), FinishReason.TOOL_CALL, ModelUsage(1, 1),
        )


async def interrupted_task(app, root: Path):
    session = await app.kernel.create_session("generic resolution")
    task = await app.kernel.create_task(
        "trace the payment ownership", root, session_id=session.session_id
    )
    for state in (
        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING, TaskState.EXECUTING,
    ):
        task = await app.kernel.transition_task(task.task_id, state, state.value)
    # A compact valid checkpoint is easiest to obtain from a failed model call
    # in the existing CLI tests; here the candidate directory contract itself
    # is exercised by assigning the resolver after a fixture checkpoint test.
    return session, task


class SessionInputResolverContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_follow_up_completed_task_wins_over_unrelated_unfinished_task(self):
        resolver = FixtureResolver({})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("mixed history")
                completed = await app.kernel.create_task(
                    "compare Codex and Hermes", root,
                    session_id=session.session_id,
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    completed = await app.kernel.transition_task(
                        completed.task_id, state, state.value
                    )
                await app.kernel.run_agent_turn(
                    completed.task_id, completed.goal
                )
                for state in (
                    TaskState.VERIFYING, TaskState.FINALIZING, TaskState.SUCCEEDED
                ):
                    completed = await app.kernel.transition_task(
                        completed.task_id, state, state.value
                    )
                unrelated = SessionResumeCandidate(
                    "task-nba", "query today's NBA news", "AWAITING_USER",
                    str(root), SessionResumeSafety.AWAIT_USER_ACTION,
                    "clarification_required",
                )
                resolver.response = {
                    "disposition": "CREATE_TASK",
                    "relation": "FOLLOW_UP",
                    "source_task_id": completed.task_id,
                    # No goal: a resolver classifies the message, and Runtime
                    # builds the derived goal from the request plus the source
                    # summary. See tests/test_derived_task_goal_authorship.py.
                    "input_grounding": "CONTEXT_DEPENDENT",
                    "confidence": 0.96,
                    "reason_code": "references_completed_result",
                    "clarification": None,
                    "candidate_task_ids": [],
                }
                with patch.object(
                    app.kernel, "list_session_resume_candidates",
                    AsyncMock(return_value=(unrelated,)),
                ):
                    decision = await app.kernel.resolve_session_input(
                        session.session_id, "那现在还需要补充哪些", root
                    )
                self.assertEqual(
                    decision.disposition, SessionRouteDisposition.CREATE_TASK,
                    (decision.reason_code, [item.to_data() for item in decision.task_catalog]),
                )
                self.assertEqual(decision.relation, SessionTaskRelation.FOLLOW_UP)
                self.assertEqual(decision.source_task_id, completed.task_id)
                self.assertEqual(
                    {item.task_id for item in decision.task_catalog},
                    {unrelated.task_id, completed.task_id},
                )
                routed_context = resolver.inputs[-1][1]
                self.assertEqual(
                    routed_context["conversation_anchor_task_id"],
                    completed.task_id,
                )
                self.assertEqual(
                    routed_context["task_catalog"][0]["task_id"],
                    completed.task_id,
                )
                self.assertEqual(
                    routed_context["task_catalog"][0]["phase1_state"],
                    "DONE",
                )
                self.assertTrue(
                    routed_context["task_catalog"][0]
                    ["is_conversation_anchor"]
                )
                self.assertFalse(next(
                    item["is_conversation_anchor"]
                    for item in routed_context["task_catalog"]
                    if item["task_id"] == unrelated.task_id
                ))
                assert decision.resolved_goal is not None
                self.assertIn("那现在还需要补充哪些", decision.resolved_goal)
                derived = await app.kernel.create_task(
                    decision.resolved_goal, root,
                    session_id=session.session_id,
                    source_task_id=decision.source_task_id,
                    task_relation=decision.relation,
                )
                self.assertEqual(derived.state, TaskState.CREATED)
                self.assertIsNone(derived.pending_approval)
                self.assertIsNone(derived.pending_clarification)
                self.assertEqual(derived.workspace_access_grants, ())
                self.assertIsNone(derived.active_agent_checkpoint)
                events = await app.kernel.dependencies.store.read_session_events(
                    session.session_id
                )
                attached = [
                    event for event in events
                    if event.event_type == "session.task_attached"
                    and event.payload["task_id"] == derived.task_id
                ][0]
                self.assertEqual(
                    attached.payload["source_task_id"], completed.task_id
                )
                self.assertEqual(
                    attached.payload["task_relation"], "FOLLOW_UP"
                )
            finally:
                await app.registry.stop_all()

    async def _resolve_with_unfinished_candidate(self, response):
        resolver = FixtureResolver(response)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        app = compose_fixture_application(
            model_adapter=EchoModelProvider(), tool_adapters=(),
            session_input_resolver_adapter=resolver,
        )
        await app.registry.start_all()
        self.addAsyncCleanup(app.registry.stop_all)
        session = await app.kernel.create_session("grounding contract")
        candidate = SessionResumeCandidate(
            "task-existing", "unfinished goal", "INTERRUPTED",
            str(root), SessionResumeSafety.EXACT_RESUME, "checkpoint",
        )
        with patch.object(
            app.kernel, "list_session_resume_candidates",
            AsyncMock(return_value=(candidate,)),
        ):
            decision = await app.kernel.resolve_session_input(
                session.session_id, "current input", root
            )
        return app, session, resolver, decision

    def test_displayed_ordinal_and_identifiers_resolve_without_model(self):
        interaction = SessionInteractionRequest(
            "interaction-1", SessionInteractionKind.CHOICE, "choose",
            tuple(
                SessionChoiceOption(
                    f"option-{index}", index, f"goal {index}", "TASK",
                    f"task-{index}", {},
                )
                for index in range(1, 5)
            ), datetime.now(timezone.utc), "test",
        )
        resolver = DeterministicSessionChoiceResolver()
        for text in ("4", "#4", "第四个", "第四个吧", "task-4"):
            with self.subTest(text=text):
                decision = resolver.select(text, interaction)
                self.assertEqual(decision.action, SessionChoiceAction.SELECT)
                self.assertEqual(decision.target_id, "task-4")
        self.assertEqual(
            resolver.select("继续 tracing 那个", interaction).action,
            SessionChoiceAction.UNRESOLVED,
        )

    async def test_pending_choice_survives_sqlite_restart_and_is_consumed(self):
        from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.db"
            candidates = tuple(
                SessionResumeCandidate(
                    f"task-{index}", f"goal {index}", "INTERRUPTED",
                    str(root), SessionResumeSafety.EXACT_RESUME, "checkpoint",
                )
                for index in range(1, 5)
            )
            first = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await first.registry.start_all()
            session = await first.kernel.create_session("durable choice")
            interaction = await first.kernel.request_session_task_choice(
                session.session_id, candidates, "choose one"
            )
            self.assertEqual(len(interaction.options), 4)
            await first.registry.stop_all()

            restarted = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                store_adapter=SQLiteRuntimeStore(database),
            )
            await restarted.registry.start_all()
            try:
                restored = await restarted.kernel.get_session(session.session_id)
                self.assertIsNotNone(restored.pending_interaction)
                decision = await restarted.kernel.resolve_pending_session_choice(
                    session.session_id, "第四个吧"
                )
                self.assertEqual(decision.target_id, "task-4")
                self.assertIsNone((
                    await restarted.kernel.get_session(session.session_id)
                ).pending_interaction)
                events = await restarted.kernel.dependencies.store.read_session_events(
                    session.session_id
                )
                self.assertEqual(
                    events[-1].event_type, "session.interaction_answered"
                )
            finally:
                await restarted.registry.stop_all()

    async def test_model_adapter_returns_only_structured_decision(self):
        model = JsonModel({
            "action": "RESUME_TASK", "task_id": "task-a",
            "input_grounding": "CONTEXT_DEPENDENT",
            "confidence": 0.94, "reason_code": "historical_reference",
            "clarification": None,
        })
        resolver = ModelSessionInputResolver(model)
        from tsm_agt.ports import AdapterContext
        await model.start(AdapterContext({}, lambda *_: None))
        await resolver.start(AdapterContext({}, lambda *_: None))
        result = await resolver.resolve_session_input(
            "go back to the investigation before the last one",
            {"unfinished_tasks": [{"task_id": "task-a"}]},
        )
        self.assertEqual(result["disposition"], "RESUME_TASK")
        self.assertEqual(result["relation"], "CONTINUE")
        self.assertEqual(result["source_task_id"], "task-a")
        self.assertEqual(result["input_grounding"], "CONTEXT_DEPENDENT")
        self.assertFalse(model.requests[-1].allow_tool_calls)
        self.assertEqual(model.requests[-1].tools, ())
        self.assertEqual(model.requests[-1].purpose.value, "SESSION_ROUTING")
        self.assertEqual(model.requests[-1].timeout_seconds, 15.0)
        self.assertEqual(model.requests[-1].max_provider_attempts, 1)
        system_text = model.requests[-1].messages[0].text
        self.assertIn(
            "Any supplied unfinished candidate may therefore be selected",
            system_text,
        )
        self.assertNotIn(
            "Only candidates whose safety is EXACT_RESUME", system_text
        )

    async def test_tool_capable_resolver_uses_structured_route_submission(self):
        model = RouteToolModel({
            "disposition": "CREATE_TASK",
            "relation": "INDEPENDENT",
            "source_task_id": None,
            # A goal is no longer part of the contract. A model that still sends
            # one must be tolerated and the key dropped, because failing strict
            # validation here would degrade an otherwise usable classification.
            "resolved_goal": "explain the current implementation",
            "input_grounding": "SELF_CONTAINED",
            "confidence": 0.98,
            "reason_code": "self_contained_question",
            "clarification": None,
            "candidate_task_ids": [],
        })
        resolver = ModelSessionInputResolver(model)
        from tsm_agt.ports import AdapterContext
        await model.start(AdapterContext({}, lambda *_: None))
        await resolver.start(AdapterContext({}, lambda *_: None))
        result = await resolver.resolve_session_input(
            "explain the current implementation", {"task_catalog": []}
        )
        self.assertEqual(result["disposition"], "CREATE_TASK")
        self.assertNotIn("resolved_goal", result)
        request = model.requests[-1]
        self.assertTrue(request.allow_tool_calls)
        self.assertEqual(
            [tool.name for tool in request.tools],
            ["session.submit_route_proposal"],
        )
        self.assertEqual(request.max_output_tokens, 512)

    async def test_awaiting_user_action_candidate_can_be_selected_as_context(self):
        resolver = FixtureResolver({
            "action": "RESUME_TASK", "task_id": "task-awaiting",
            "input_grounding": "CONTEXT_DEPENDENT",
            "confidence": 0.99, "reason_code": "pending_task_reference",
            "clarification": None,
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("awaiting selection")
                candidate = SessionResumeCandidate(
                    "task-awaiting", "pending goal", "AWAITING_APPROVAL",
                    str(root), SessionResumeSafety.AWAIT_USER_ACTION,
                    "explicit_approval_decision_required",
                )
                with patch.object(
                    app.kernel, "list_session_resume_candidates",
                    AsyncMock(return_value=(candidate,)),
                ):
                    decision = await app.kernel.resolve_session_input(
                        session.session_id, "continue pending work", root
                    )
                self.assertEqual(decision.action, SessionInputAction.NEW_TASK)
                self.assertEqual(
                    decision.disposition, SessionRouteDisposition.CREATE_TASK
                )
                self.assertEqual(
                    decision.relation, SessionTaskRelation.FOLLOW_UP
                )
                self.assertEqual(decision.source_task_id, "task-awaiting")
                self.assertIn("[session-follow-up]", decision.resolved_goal)
                self.assertIn("continue pending work", decision.resolved_goal)
                self.assertEqual(
                    decision.candidates[0].safety,
                    SessionResumeSafety.AWAIT_USER_ACTION,
                )
            finally:
                await app.registry.stop_all()

    async def test_old_clarification_prose_is_not_replayed_to_semantic_resolver(self):
        resolver = FixtureResolver({
            "action": "RESUME_TASK", "task_id": "task-awaiting",
            "input_grounding": "CONTEXT_DEPENDENT",
            "confidence": 0.99, "reason_code": "pending_task_reference",
            "clarification": None,
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("stale prompt isolation")
                candidate = SessionResumeCandidate(
                    "task-awaiting", "pending goal", "AWAITING_APPROVAL",
                    str(root), SessionResumeSafety.AWAIT_USER_ACTION,
                    "explicit_approval_decision_required",
                )
                stale_prompt = (
                    "old model prose: choose 1 to continue or 2 to change scope"
                )
                await app.kernel.request_session_task_choice(
                    session.session_id, (candidate,), stale_prompt
                )
                with patch.object(
                    app.kernel, "list_session_resume_candidates",
                    AsyncMock(return_value=(candidate,)),
                ):
                    decision = await app.kernel.resolve_session_input(
                        session.session_id, "continue with a narrower scope", root
                    )
                self.assertEqual(decision.action, SessionInputAction.NEW_TASK)
                self.assertEqual(
                    decision.relation, SessionTaskRelation.FOLLOW_UP
                )
                self.assertEqual(decision.source_task_id, "task-awaiting")
                routed_context = resolver.inputs[-1][1]
                pending = routed_context["pending_interaction"]
                self.assertNotIn("prompt", pending)
                self.assertNotIn("label", pending["options"][0])
                self.assertNotIn(stale_prompt, json.dumps(pending))
                self.assertEqual(
                    pending["options"][0]["target_id"], "task-awaiting"
                )
            finally:
                await app.registry.stop_all()

    async def test_unanchored_context_dependent_input_degrades_without_menu(self):
        app, session, _resolver, decision = (
            await self._resolve_with_unfinished_candidate({
                "action": "NEW_TASK", "task_id": None,
                "input_grounding": "CONTEXT_DEPENDENT",
                "confidence": 0.99, "reason_code": "model_new_task",
                "clarification": None,
            })
        )
        self.assertEqual(decision.action, SessionInputAction.NEW_TASK)
        self.assertEqual(decision.relation, SessionTaskRelation.CONTEXTUAL)
        self.assertEqual(
            decision.reason_code, "semantic_clarification_unanchored_degraded"
        )
        self.assertEqual(decision.candidate_task_ids, ())
        self.assertEqual(
            await app.kernel.list_session_tasks(session.session_id), ()
        )

    async def test_self_contained_input_may_become_new_task(self):
        _app, _session, _resolver, decision = (
            await self._resolve_with_unfinished_candidate({
                "action": "NEW_TASK", "task_id": None,
                "input_grounding": "SELF_CONTAINED",
                "confidence": 0.99, "reason_code": "independent_goal",
                "clarification": None,
            })
        )
        self.assertEqual(decision.action, SessionInputAction.NEW_TASK)

    async def test_genuine_multi_task_ambiguity_preserves_specific_choice(self):
        resolver = FixtureResolver({
            "disposition": "CLARIFY",
            "relation": "UNCERTAIN",
            "source_task_id": None,
            "input_grounding": "AMBIGUOUS",
            "confidence": 0.55,
            "reason_code": "two_plausible_referents",
            "clarification": "请选择要继续的调查。",
            "candidate_task_ids": ["task-a", "task-b"],
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("real ambiguity")
                for task_id in ("task-a", "task-b"):
                    task = await app.kernel.create_task(
                        f"investigate {task_id}", root, task_id=task_id,
                        session_id=session.session_id,
                    )
                    for state in (
                        TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                        TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                        TaskState.EXECUTING,
                    ):
                        task = await app.kernel.transition_task(
                            task.task_id, state, state.value
                        )
                candidates = tuple(
                    SessionResumeCandidate(
                        task_id, f"investigate {task_id}", "INTERRUPTED",
                        str(root), SessionResumeSafety.EXACT_RESUME, "checkpoint",
                    )
                    for task_id in ("task-a", "task-b")
                )
                with patch.object(
                    app.kernel, "list_session_resume_candidates",
                    AsyncMock(return_value=candidates),
                ):
                    decision = await app.kernel.resolve_session_input(
                        session.session_id, "继续之前那个调查", root
                    )
                self.assertEqual(decision.action, SessionInputAction.CLARIFY)
                self.assertEqual(
                    decision.candidate_task_ids, ("task-a", "task-b")
                )
            finally:
                await app.registry.stop_all()

    async def test_router_timeout_keeps_the_message_instead_of_asking_again(self):
        """A slow classifier is not evidence that the user was unclear.

        Answering a timeout with CLARIFY throws away input Runtime already
        accepted and makes the user restate a request the Agent could have
        resolved from durable Session history.
        """
        class TimingOutResolver(FixtureResolver):
            async def resolve_session_input(self, text, context):
                self.inputs.append((text, context))
                raise TimeoutError("semantic router deadline exceeded")

        resolver = TimingOutResolver({})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("router timeout")
                candidate = SessionResumeCandidate(
                    "task-existing", "unfinished goal", "INTERRUPTED",
                    str(root), SessionResumeSafety.EXACT_RESUME, "checkpoint",
                )
                with patch.object(
                    app.kernel, "list_session_resume_candidates",
                    AsyncMock(return_value=(candidate,)),
                ):
                    decision = await app.kernel.resolve_session_input(
                        session.session_id,
                        "但是我们现在没有后端介入，但是要预留这个功能", root,
                    )

                self.assertEqual(decision.action, SessionInputAction.NEW_TASK)
                self.assertEqual(
                    decision.relation, SessionTaskRelation.CONTEXTUAL
                )
                self.assertEqual(
                    decision.reason_code,
                    "semantic_router_timeout_contextual_fallback",
                )
                self.assertIsNone(decision.clarification)
                # The Agent still receives the original text plus Session
                # history, so the thread is not lost.
                self.assertEqual(
                    decision.resolved_goal,
                    "但是我们现在没有后端介入，但是要预留这个功能",
                )
                # A timeout must never be answered by resuming a guessed Task.
                self.assertIsNone(decision.source_task_id)
            finally:
                await app.registry.stop_all()

    async def test_missing_grounding_degrades_to_contextual_task(self):
        _app, _session, _resolver, decision = (
            await self._resolve_with_unfinished_candidate({
                "action": "NEW_TASK", "task_id": None,
                "confidence": 0.99, "reason_code": "legacy_response",
                "clarification": None,
            })
        )
        self.assertEqual(decision.action, SessionInputAction.NEW_TASK)
        self.assertEqual(decision.relation, SessionTaskRelation.CONTEXTUAL)
        self.assertEqual(
            decision.reason_code,
            "semantic_router_protocol_contextual_fallback",
        )
        self.assertEqual(decision.candidate_task_ids, ())

    async def test_semantic_resolver_has_one_short_cancellable_deadline(self):
        class SlowModel(JsonModel):
            async def complete(self, request):
                self.requests.append(request)
                await asyncio.sleep(2)
                return await super().complete(request)

        model = SlowModel({})
        resolver = ModelSessionInputResolver(model, timeout_seconds=0.01)
        from tsm_agt.ports import AdapterContext
        await model.start(AdapterContext({}, lambda *_: None))
        await resolver.start(AdapterContext({}, lambda *_: None))
        with self.assertRaises(TimeoutError):
            await resolver.resolve_session_input("ambiguous", {
                "unfinished_tasks": []
            })
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.requests[0].max_provider_attempts, 1)

    async def test_no_history_bypasses_semantic_resolver(self):
        resolver = FixtureResolver({})
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("empty")
                decision = await app.kernel.resolve_session_input(
                    session.session_id, "any ordinary input", Path(directory)
                )
                self.assertEqual(decision.action, SessionInputAction.NEW_TASK)
                self.assertEqual(resolver.inputs, [])
                events = await app.kernel.dependencies.store.read_session_events(
                    session.session_id
                )
                resolved = events[-1]
                self.assertEqual(resolved.event_type, "session.input_resolved")
                self.assertNotIn("any ordinary input", str(resolved.payload))
                self.assertIn("text_hash", resolved.payload)
            finally:
                await app.registry.stop_all()

    async def test_invalid_selection_never_resumes_and_degrades_safely(self):
        # Kernel validates Resolver output; the Adapter cannot invent a Task ID
        # or turn a weak guess into execution authority.
        from tsm_agt.core import (
            AgentTurnCheckpoint, SessionResumeCandidate, SessionResumeSafety,
        )
        resolver = FixtureResolver({
            "action": "RESUME_TASK", "task_id": "task-invented",
            "input_grounding": "CONTEXT_DEPENDENT",
            "confidence": 0.99, "reason_code": "guess",
            "clarification": None,
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("invalid selection")
                task = await app.kernel.create_task(
                    "unfinished goal", root, session_id=session.session_id
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await app.kernel.transition_task(
                        task.task_id, state, state.value
                    )
                checkpoint = AgentTurnCheckpoint(
                    task.task_id, "turn-fixture", 1, (), (), (),
                    0, 0, 0, 0, 2, 2, 128, 1.0,
                    task.project_fingerprint,
                    task.effective_configurations[-1].effective_config_hash,
                    "unused", task.effective_configurations[-1].prompt_manifest_hash or "",
                    session.session_id, (await app.kernel.get_session(session.session_id)).context_hash,
                    (await app.kernel.get_working_memory(task.task_id)).content_hash,
                )
                stored = await app.kernel.dependencies.store.load_task(task.task_id)
                assert stored is not None
                # Use the Kernel checkpoint writer so the candidate remains a
                # normal authoritative Task snapshot.
                await app.kernel._save_agent_checkpoint(checkpoint, "fixture")
                decision = await app.kernel.resolve_session_input(
                    session.session_id, "refer to some earlier work", root
                )
                self.assertEqual(
                    decision.action, SessionInputAction.NEW_TASK
                )
                self.assertEqual(
                    decision.relation, SessionTaskRelation.CONTEXTUAL
                )
                self.assertIsNone(decision.source_task_id)
                self.assertEqual(
                    decision.reason_code,
                    "semantic_router_protocol_contextual_fallback",
                )
            finally:
                await app.registry.stop_all()


class SessionAnswerTest(unittest.IsolatedAsyncioTestCase):
    async def test_current_input_is_the_final_standalone_user_message(self):
        model = JsonModel({"answer": "current request handled"})
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=model, tool_adapters=(),
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("answer prompt")
                for index in range(7):
                    await app.kernel.record_session_answer(
                        session.session_id,
                        f"historical user request {index}",
                        f"historical assistant answer {index}",
                    )

                current = (
                    "为什么 Trace 节点显示 running，实际不是已经执行完了吗？"
                )
                await app.kernel.answer_session_message(
                    session.session_id, current
                )

                request = model.requests[-1]
                self.assertEqual(
                    [message.role for message in request.messages],
                    [MessageRole.SYSTEM, MessageRole.USER, MessageRole.USER],
                )
                self.assertEqual(request.messages[-1].text, current)
                history = json.loads(request.messages[-2].text)
                self.assertEqual(
                    history["boundary"], "untrusted_session_history"
                )
                self.assertEqual(len(history["recent_messages"]), 12)
                self.assertNotIn(
                    current,
                    [item["text"] for item in history["recent_messages"]],
                )
                self.assertNotIn("current_input", history)
                self.assertIn("final User message", request.messages[0].text)
                # The only Tool offered grants no capability: it exists so the
                # model can report that this route cannot serve the message.
                self.assertEqual(
                    [tool.name for tool in request.tools],
                    ["session.requires_agent_task"],
                )
                self.assertTrue(request.tools[0].is_read_only)
                self.assertTrue(request.tools[0].is_internal_state)
                self.assertTrue(request.allow_tool_calls)
            finally:
                await app.registry.stop_all()

    async def test_self_contained_answer_bypasses_task_creation(self):
        resolver = FixtureResolver({
            "disposition": "ANSWER",
            "relation": "INDEPENDENT",
            "source_task_id": None,
            "input_grounding": "SELF_CONTAINED",
            "confidence": 0.96,
            "reason_code": "self_contained_question",
            "clarification": None,
            "candidate_task_ids": [],
        })
        with tempfile.TemporaryDirectory() as directory:
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
                session_input_resolver_adapter=resolver,
            )
            await app.registry.start_all()
            try:
                session = await app.kernel.create_session("answer only")
                history_task = await app.kernel.create_task(
                    "completed context", Path(directory),
                    session_id=session.session_id,
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING, TaskState.VERIFYING,
                    TaskState.FINALIZING, TaskState.SUCCEEDED,
                ):
                    history_task = await app.kernel.transition_task(
                        history_task.task_id, state, state.value
                    )
                decision = await app.kernel.resolve_session_input(
                    session.session_id, "What is a Session?", Path(directory)
                )
                self.assertEqual(
                    decision.disposition, SessionRouteDisposition.ANSWER
                )
                self.assertEqual(
                    len(await app.kernel.list_session_tasks(session.session_id)), 1
                )
                await app.kernel.record_session_answer(
                    session.session_id, "What is a Session?",
                    "A Session is a durable conversation container.",
                )
                conversation = await app.kernel.get_session_conversation(
                    session.session_id
                )
                self.assertEqual(
                    [item.text for item in conversation.messages],
                    [
                        "What is a Session?",
                        "A Session is a durable conversation container.",
                    ],
                )
                self.assertTrue(all(
                    item.task_id is None for item in conversation.messages
                ))
            finally:
                await app.registry.stop_all()
