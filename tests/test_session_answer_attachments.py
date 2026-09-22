"""An image sent with an ordinary message must reach the direct-answer model.

Not every attachment implies workspace work: "explain the problem in this
screenshot" is answerable without any Tool. The direct-answer route used to take
only text, so the image was dropped silently and the model replied by asking for
a screenshot the user had already attached. These tests pin the image reaching
the model, the prompt telling it the image is there, and the tool-free and
no-base64-in-history invariants that route still has to keep.
"""

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import TaskState
from tsm_agt.ports import (
    AdapterDescriptor, FinishReason, HealthState, HealthStatus, ImageBlock,
    Message, MessageRole, ModelResponse, ModelUsage, ProviderCapabilities,
    TextBlock,
)
from tsm_agt.sdk import EngineeringAgentClient

PIXEL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGP4z8AAAAMBAQAY"
    "3Y2wAAAAAElFTkSuQmCC"
)


class AnswerRouteResolver:
    """Route every message to ANSWER, the path under test."""

    descriptor = AdapterDescriptor(
        "fixture.answer-route", "1.0", "SessionInputResolverPort", "1.0",
    )

    async def start(self, context): pass
    async def health(self): return HealthStatus(HealthState.HEALTHY)
    async def stop(self, deadline): pass

    async def resolve_session_input(self, _text, _context):
        return {
            "disposition": "ANSWER",
            "relation": "INDEPENDENT",
            "source_task_id": None,
            "resolved_goal": None,
            "input_grounding": "SELF_CONTAINED",
            "confidence": 0.97,
            "reason_code": "self_contained_image_question",
            "clarification": None,
            "candidate_task_ids": [],
        }


class RecordingModel(EchoModelProvider):
    """Captures every ModelRequest so the sent blocks can be inspected."""

    capabilities = ProviderCapabilities(
        tools=True, vision=True, context_window=8192,
    )

    def __init__(self) -> None:
        super().__init__()
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            Message(
                f"answer-{len(self.requests)}", MessageRole.ASSISTANT,
                (TextBlock("图里这段间距不一致，是 toolbar 的内边距被覆盖了。"),),
            ), FinishReason.STOP, ModelUsage(1, 1),
        )


class SessionAnswerAttachmentsTest(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, root: Path, model, *, resolver=None):
        app = compose_fixture_application(
            model_adapter=model, tool_adapters=(),
            session_input_resolver_adapter=resolver,
        )
        client = EngineeringAgentClient(root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client

    @staticmethod
    async def seed_history(client, root: Path, session_id: str) -> None:
        """Give the Session one catalog entry so routing consults the resolver."""
        task = await client.application.kernel.create_task(
            "earlier work", root, session_id=session_id,
        )
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING, TaskState.VERIFYING,
            TaskState.FINALIZING, TaskState.SUCCEEDED,
        ):
            await client.application.kernel.transition_task(
                task.task_id, state, state.value
            )

    @staticmethod
    def current_user(request) -> Message:
        """The answer route always sends system, history, then the real message."""
        return request.messages[2]

    async def test_attachment_reaches_the_answering_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = RecordingModel()
            client = await self.make_client(Path(directory), model)
            session = await client.application.kernel.create_session("image")

            await client.application.kernel.answer_session_message(
                session.session_id, "这一块有方法改成可视化更好的格式吗",
                attachments=(ImageBlock(image_url=PIXEL),),
            )

            content = self.current_user(model.requests[0]).content
            self.assertIsInstance(content[0], TextBlock)
            images = [b for b in content if isinstance(b, ImageBlock)]
            self.assertEqual([image.image_url for image in images], [PIXEL])

    async def test_prompt_tells_the_model_the_image_is_already_attached(self) -> None:
        """Without this the model answers by asking for the attached screenshot."""
        with tempfile.TemporaryDirectory() as directory:
            model = RecordingModel()
            client = await self.make_client(Path(directory), model)
            session = await client.application.kernel.create_session("image")

            await client.application.kernel.answer_session_message(
                session.session_id, "看下图里的问题",
                attachments=(ImageBlock(image_url=PIXEL),),
            )

            system = model.requests[0].messages[0].text
            self.assertIn("1 attached image(s)", system)
            self.assertIn("already attached", system)
            # Image text is content, never instruction.
            self.assertIn("not instructions", system)

    async def test_plain_answer_prompt_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = RecordingModel()
            client = await self.make_client(Path(directory), model)
            session = await client.application.kernel.create_session("plain")

            await client.application.kernel.answer_session_message(
                session.session_id, "什么是 Session？",
            )

            system = model.requests[0].messages[0].text
            self.assertNotIn("attached image", system)
            content = self.current_user(model.requests[0]).content
            self.assertEqual(len(content), 1)
            self.assertIsInstance(content[0], TextBlock)

    async def test_session_text_forwards_images_to_the_answer_route(self) -> None:
        """The SDK ANSWER branch used to drop the images it had already parsed."""
        with tempfile.TemporaryDirectory() as directory:
            model = RecordingModel()
            client = await self.make_client(
                Path(directory), model, resolver=AnswerRouteResolver(),
            )
            kernel = client.application.kernel
            session = await kernel.create_session("forward")
            await self.seed_history(client, Path(directory), session.session_id)

            result = await client.submit_session_text(
                session.session_id, "解释下图里这段的问题",
                command_id="request-image",
                images=(ImageBlock(image_url=PIXEL),),
            )

            self.assertEqual(result.result["kind"], "answer")
            answer_request = next(
                request for request in model.requests
                if any(
                    isinstance(block, ImageBlock)
                    for message in request.messages
                    for block in message.content
                )
            )
            images = [
                block for block in self.current_user(answer_request).content
                if isinstance(block, ImageBlock)
            ]
            self.assertEqual([image.image_url for image in images], [PIXEL])

    async def test_answered_image_turn_keeps_base64_out_of_session_history(
        self,
    ) -> None:
        """Session history is a text projection; base64 must not leak into it."""
        with tempfile.TemporaryDirectory() as directory:
            model = RecordingModel()
            client = await self.make_client(Path(directory), model)
            kernel = client.application.kernel
            session = await kernel.create_session("history")

            await kernel.answer_session_message(
                session.session_id, "看下图里的问题",
                attachments=(ImageBlock(image_url=PIXEL),),
            )

            events = await kernel.dependencies.store.read_session_events(
                session.session_id
            )
            turn = next(
                event for event in events
                if event.event_type == "session.chat_turn_recorded"
            )
            self.assertNotIn("base64", str(turn.payload))
            conversation = await kernel.get_session_conversation(
                session.session_id
            )
            self.assertEqual(
                [item.text for item in conversation.messages][0],
                "看下图里的问题",
            )


if __name__ == "__main__":
    unittest.main()
