from __future__ import annotations

import unittest

from tsm_agt.ports import (
    AssistantMessagePhase,
    Message,
    MessageRole,
    TextBlock,
    ToolCall,
    ToolCallBlock,
    ToolResult,
    ToolResultBlock,
)


class ModelMessageTest(unittest.TestCase):
    def test_message_text_joins_ordered_text_blocks(self) -> None:
        message = Message(
            message_id="msg-1",
            role=MessageRole.USER,
            content=(TextBlock("hello "), TextBlock("world")),
        )

        self.assertEqual(message.text, "hello world")
        self.assertEqual(
            message.to_data(),
            {
                "message_id": "msg-1",
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"},
                ],
            },
        )

    def test_message_serializes_tool_blocks_without_treating_them_as_text(self) -> None:
        message = Message(
            message_id="msg-2",
            role=MessageRole.ASSISTANT,
            content=(
                TextBlock("checking"),
                ToolCallBlock(ToolCall("call-1", "fixture.echo", {"text": "hi"})),
                ToolResultBlock(ToolResult("call-1", True, data={"text": "hi"})),
            ),
        )

        self.assertEqual(message.text, "checking")
        self.assertEqual(message.to_data()["content"][1]["type"], "tool_call")
        self.assertEqual(message.to_data()["content"][2]["type"], "tool_result")

    def test_assistant_phase_round_trips_and_is_assistant_only(self) -> None:
        message = Message(
            message_id="msg-commentary",
            role=MessageRole.ASSISTANT,
            content=(TextBlock("我会先检查配置。"),),
            phase=AssistantMessagePhase.COMMENTARY,
        )

        self.assertEqual(message.to_data()["phase"], "commentary")
        self.assertEqual(Message.from_data(message.to_data()), message)
        with self.assertRaisesRegex(ValueError, "only assistant messages"):
            Message(
                "not-assistant", MessageRole.USER, (TextBlock("hi"),),
                phase=AssistantMessagePhase.COMMENTARY,
            )


if __name__ == "__main__":
    unittest.main()
