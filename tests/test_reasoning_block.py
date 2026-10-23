from tsm_agt.ports.model import Message, MessageRole, ReasoningBlock, TextBlock


def test_reasoning_block_round_trip_does_not_become_answer_text():
    message = Message("m", MessageRole.ASSISTANT, (
        ReasoningBlock("anthropic", "thinking", "summary", "signature"),
        ReasoningBlock("anthropic", "redacted_thinking", data="opaque"),
        TextBlock("answer"),
    ))
    restored = Message.from_data(message.to_data())
    assert restored == message
    assert restored.text == "answer"
