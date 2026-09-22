"""Task result text must survive an SDK/Web process restart."""

import tempfile
import unittest
from pathlib import Path

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.sdk import EngineeringAgentClient


class TaskResultTextRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_intermediate_llm_text_is_not_misreported_as_a_final_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = compose_fixture_application(
                model_adapter=EchoModelProvider(), tool_adapters=(),
            )
            client = EngineeringAgentClient(root, application_factory=lambda: app)
            await client.start()
            self.addAsyncCleanup(client.close)
            task = await app.kernel.create_task("inspect the renderer", root)
            await app.kernel._append_events(task.task_id, ((
                "llm.completed",
                {
                    "turn_id": "turn-text",
                    "message": {
                        "message_id": "answer-text", "role": "assistant",
                        "content": [{
                            "type": "text",
                            "text": "这段已生成的正文必须在重启后仍能显示。",
                        }],
                    },
                    "finish_reason": "stop",
                },
            ),))
            # This represents a newly started Web/SDK process: no live result
            # cache remains, only the durable Runtime event log.
            client._latest_results.clear()

            result = await client.get_task_result(task.task_id)

            # A raw llm.completed can be a provisional model summary. It is
            # not a user-facing Task result until Session records it.
            self.assertIsNone(result.assistant_text)


if __name__ == "__main__":
    unittest.main()
