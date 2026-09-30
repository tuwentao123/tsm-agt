"""The planner authors the Task contract, so its prompt owns the trust rules.

A planner that invents constraints can forbid exactly what the user asked for:
in ``task-6991211729074344912e9cccb81a7d80`` the user said "you can query the
链路 data from runtime.db", and the Task SPEC the planner wrote answered with
``不得假定已访问或修改本地 runtime.db``. The Agent then obeyed its own contract,
made zero tool calls, and explained that it was not allowed to look.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.adapters.model_task_spec_planner import ModelTaskSpecPlanner
from tsm_agt.ports import (
    AdapterContext, FinishReason, Message, MessageRole, ModelResponse,
    ProviderCapabilities, TextBlock,
)


def _proposal() -> dict:
    return {
        "schema_version": 1,
        "goal": "query the runtime ledger for the task's execution chain",
        "scope": ["read .agent/runtime.db"],
        "constraints": [],
        "acceptance_criteria": [{
            "criterion_id": "ac-chain",
            "description": "the ledger was queried and its rows are cited",
            "verification_kind": "rubric",
        }],
        "outcomes": [{
            "outcome_id": "answer",
            "description": "report the chain read from the ledger",
            "kind": "ANSWER",
            "required_effects": ["observe"],
            "required": True,
        }],
        "continuation_policy": {"mode": "NONE"},
    }


class CapturingPlannerModel(EchoModelProvider):
    """Text-JSON planner path, so the whole system prompt is observable."""

    capabilities = ProviderCapabilities(tools=False, context_window=8192)

    def __init__(self) -> None:
        super().__init__()
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            Message(
                "planner-proposal", MessageRole.ASSISTANT,
                (TextBlock(json.dumps(_proposal())),),
            ),
            FinishReason.STOP,
        )


class PlannerContractPromptTest(unittest.IsolatedAsyncioTestCase):
    async def _prompt(self) -> str:
        model = CapturingPlannerModel()
        planner = ModelTaskSpecPlanner(model)
        await planner.start(AdapterContext({}, lambda *_: None))
        try:
            await planner.propose_task_spec("query runtime.db for the chain", {})
        finally:
            await planner.stop(datetime.now())
        self.assertTrue(model.requests, "planner never called the model")
        return "\n".join(
            block.text
            for message in model.requests[0].messages
            for block in message.content
            if isinstance(block, TextBlock)
        )

    async def test_constraints_may_not_contradict_the_user(self) -> None:
        prompt = await self._prompt()
        self.assertIn("never invent a restriction", prompt)
        self.assertIn("drop the constraint and keep the request", prompt)

    async def test_a_limitation_from_context_may_not_become_a_constraint(
        self,
    ) -> None:
        prompt = await self._prompt()
        self.assertIn(
            "into scope, constraints, or acceptance criteria", prompt,
        )
        self.assertIn(
            "Never turn a limitation you saw in an earlier reply", prompt,
        )

    async def test_a_named_data_source_becomes_required_work(self) -> None:
        prompt = await self._prompt()
        self.assertIn("query runtime.db", prompt)
        self.assertIn("carrying the observe (or execute) effect", prompt)


if __name__ == "__main__":
    unittest.main()
