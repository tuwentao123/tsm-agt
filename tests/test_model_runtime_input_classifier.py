from __future__ import annotations

import json
import unittest

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.model_runtime_input import ModelRuntimeInputClassifier
from tsm_agt.ports import (
    AdapterContext, FinishReason, Message, MessageRole, ModelResponse,
    ModelUsage, TextBlock,
)


class JsonModel(EchoModelProvider):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            Message(
                "runtime-resolver-result", MessageRole.ASSISTANT,
                (TextBlock(json.dumps(self.data)),),
            ), FinishReason.STOP, ModelUsage(1, 1),
        )


class ModelRuntimeInputClassifierTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_structured_decision_without_tools(self):
        model = JsonModel({"intent": "STEER", "confidence": 0.96})
        classifier = ModelRuntimeInputClassifier(model)
        context = AdapterContext({}, lambda *_: None)
        await model.start(context)
        await classifier.start(context)

        result = await classifier.classify_runtime_input(
            "keep the current work but add Windows coverage",
            {"task_state": "EXECUTING", "current_goal": "repair CLI"},
        )

        self.assertEqual(result["intent"], "STEER")
        request = model.requests[-1]
        self.assertFalse(request.allow_tool_calls)
        self.assertEqual(request.tools, ())
        self.assertIn("repair CLI", request.messages[-1].text)
        self.assertIn(
            "REVIEW_PENDING_ACTION", request.messages[0].text
        )
