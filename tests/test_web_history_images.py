"""History attachments come from durable inputs, not browser memory."""
from datetime import datetime, timezone
from unittest import IsolatedAsyncioTestCase, mock

from tests.test_web_session_restore import (
    _FakeKernel, _FakeRuntimeServer, _Message, _Projection,
    _Session, _SessionEvent, web_app,
)
from tsm_agt.ports import MessageRole


class HistoryImagesTest(IsolatedAsyncioTestCase):
    async def history(self, messages, events):
        session = _Session("s", "images", (), None, datetime.now(timezone.utc))
        kernel = _FakeKernel((session,), {}, _Projection(tuple(messages)))
        kernel.dependencies.store._events = tuple(events)
        with mock.patch.object(web_app, "runtime_server", _FakeRuntimeServer(kernel, {})):
            return (await web_app.get_session("s"))["messages"]

    def event(self, sequence, kind, task, turn, url):
        return _SessionEvent(sequence, kind, {
            "task_id": task, "turn_id": turn,
            "user_message": {
                "message_id": f"msg-{task}-{turn}", "role": "user",
                "content": [{"type": "text", "text": "看图"},
                            {"type": "input_image", "image_url": url}],
            },
        })

    async def test_cold_history_restores_each_turn_without_duplicates(self):
        first = "data:image/png;base64,Zmlyc3Q="
        second = "data:image/png;base64,c2Vjb25k"
        messages = [
            _Message(MessageRole.USER, "看图", "task", 2, "one"),
            _Message(MessageRole.ASSISTANT, "回复", "task", 3, "one"),
            _Message(MessageRole.USER, "看图", "task", 4, "two"),
            _Message(MessageRole.USER, "无图", "other", 6, "one"),
        ]
        events = [
            self.event(2, "session.turn_input_recorded", "task", "one", first),
            self.event(3, "session.task_result_recorded", "task", "one", first),
            self.event(4, "session.turn_input_recorded", "task", "two", second),
        ]
        rows = await self.history(messages, events)
        self.assertEqual([image["image_url"] for image in rows[0]["images"]], [first])
        self.assertEqual(rows[1]["images"], [])
        self.assertEqual([image["image_url"] for image in rows[2]["images"]], [second])
        self.assertEqual(rows[3]["images"], [])

    async def test_legacy_attachment_gets_images_from_first_result(self):
        url = "data:image/png;base64,bGVnYWN5"
        rows = await self.history(
            [_Message(MessageRole.USER, "看图", "task", 1)],
            [_SessionEvent(1, "session.task_attached", {"task_id": "task", "user_text": "看图"}),
             self.event(3, "session.task_result_recorded", "task", "one", url)],
        )
        self.assertEqual(rows[0]["turn_id"], "one")
        self.assertEqual(rows[0]["images"][0]["image_url"], url)

    async def test_explicit_empty_input_wins_over_repeated_result_images(self):
        input_event = self.event(2, "session.turn_input_recorded", "task", "one", "unused")
        input_event.payload["user_message"]["content"] = [{"type": "text", "text": "无图"}]
        rows = await self.history(
            [_Message(MessageRole.USER, "无图", "task", 2, "one")],
            [self.event(3, "session.task_result_recorded", "task", "one", "stale"), input_event],
        )
        self.assertEqual(rows[0]["images"], [])

    async def test_multiple_images_keep_input_order_without_result_duplicates(self):
        event = self.event(2, "session.turn_input_recorded", "task", "one", "first")
        event.payload["user_message"]["content"].extend([
            {"type": "input_image", "image_url": "second"},
            {"type": "input_image", "image_url": None},
            {"type": "text", "text": "third"},
        ])
        rows = await self.history(
            [_Message(MessageRole.USER, "看图", "task", 2, "one")],
            [event, self.event(3, "session.task_result_recorded", "task", "one", "first")],
        )
        self.assertEqual(rows[0]["images"], [{"image_url": "first"}, {"image_url": "second"}])

    async def test_non_input_events_cannot_supply_history_images(self):
        rows = await self.history(
            [_Message(MessageRole.USER, "无图", "task", 2, "one")],
            [self.event(2, "session.context_state_updated", "task", "one", "https://example.com/image.png")],
        )
        self.assertEqual(rows[0]["images"], [])
