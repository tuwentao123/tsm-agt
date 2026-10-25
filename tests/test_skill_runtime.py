"""End-to-end tests for the Skill runtime integration.

This test verifies that:

* a CODE skill registered in SkillRegistry and enabled on a Session
  is exposed to the model as a tool by the ConversationAgentLoop;
* a tool call whose name matches the skill name is routed through
  ``turn_tool_batch`` to the skill handler;
* the resulting ToolResult is marked as a ``skill`` runtime kind.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core.skills import Skill, SkillKind
from tsm_agt.ports import (
    AdapterDescriptor,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderCapabilities,
    RuntimeStorePort,
    FinishReason,
    TextBlock,
    ToolCall,
    ToolCallBlock,
)
from tsm_agt.adapters.fixture import EchoModelProvider
from tests.test_conversation_loop import _executing


class ScriptedModel(EchoModelProvider):
    """Deterministic provider that plays a fixed list of responses.

    This is a minimal clone of the fixture used in the conversation loop
    tests, specialised for the skill runtime test.
    """

    capabilities = ProviderCapabilities(tools=True, context_window=32_000)
    descriptor = AdapterDescriptor(
        "fixture.skill-scripted-model", "1.0", "ModelProviderPort", "1.0"
    )

    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("scripted model ran out of responses")
        return self._responses.pop(0)


def _tool_call_response(call_id: str, name: str, arguments: Mapping[str, Any]) -> ModelResponse:
    call = ToolCall(call_id, name, dict(arguments))
    return ModelResponse(
        Message("skill-call", MessageRole.ASSISTANT, (ToolCallBlock(call),)),
        FinishReason.TOOL_CALL,
        ModelUsage(5, 4),
    )


def _text_response(message_id: str, text: str) -> ModelResponse:
    return ModelResponse(
        Message(message_id, MessageRole.ASSISTANT, (TextBlock(text),)),
        FinishReason.STOP,
        ModelUsage(5, 4),
    )


class SkillRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_composition_wires_selector_to_runtime_registry(self) -> None:
        application = compose_fixture_application()
        dependencies = application.kernel._dependencies
        self.assertIsNotNone(dependencies.skill_selector)
        self.assertIs(dependencies.skill_selector.registry, dependencies.skill_registry)

    async def test_code_skill_is_exposed_and_executes_via_tool_call(self) -> None:
        """A CODE skill enabled on the Session runs when the model calls it.

        The ToolResult emitted by the runtime must carry ``runtime_kind ==
        "skill"`` in its metadata so higher layers can distinguish it from
        ordinary tool providers.
        """

        # Track invocations to prove the handler ran.
        invocations: list[Mapping[str, Any]] = []

        async def echo_skill(args: Mapping[str, Any]) -> Mapping[str, Any]:
            invocations.append(dict(args))
            return {"echo": args.get("text")}

        # Define a simple CODE skill.
        skill = Skill(
            id="code.demo_echo",
            name="demo.echo",
            description="Echo back the provided text.",
            kind=SkillKind.CODE,
            args_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
            },
            handler=echo_skill,
            plan_path=None,
            tags=("demo", "echo"),
            safety_level="read_only",
            approval_required=False,
        )

        # The scripted model will first issue a tool call for the skill, then
        # return a plain text answer to end the turn.
        model = ScriptedModel([
            _tool_call_response("skill-call-1", skill.name, {"text": "hello"}),
            _text_response("final", "done"),
        ])

        application = compose_fixture_application(model_adapter=model, tool_adapters=(), enable_skills=True)
        await application.registry.start_all()
        self.addAsyncCleanup(application.registry.stop_all)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Create a Task and obtain its Session.
            task = await _executing(application, root)
            session = await application.kernel.get_session(task.session_id)

            # Register the skill and enable it on the Session.
            registry = application.kernel._dependencies.skill_registry
            registry.register(skill)
            enabled = (skill.id,)
            await application.kernel.update_session_skills(session.session_id, enabled)

            # Run one agent turn that should use the skill.
            await application.kernel.run_agent_turn(
                task.task_id,
                "请使用 demo.echo 技能返回 echo",
            )

            # The handler must have been invoked with the tool-call arguments.
            self.assertEqual(invocations, [{"text": "hello"}])

            # A tool.completed event should exist whose result meta marks this
            # as a skill runtime.
            store = application.registry.require(RuntimeStorePort)
            events = await store.read_events(task.task_id)
            skill_completed = [
                event
                for event in events
                if event.event_type == "tool.completed"
                and event.payload.get("result", {}).get("meta", {}).get("runtime_kind")
                == "skill"
            ]
            self.assertTrue(skill_completed, "expected a skill-completed ToolResult event")

