"""Unknown results are model-visible facts, not Runtime control decisions."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
import tempfile

from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import AgentTurnResult, ToolCommitState
from tsm_agt.ports import ToolCall, ToolCallBlock, ToolRecoveryKind, ToolResultBlock
from tests.test_conversation_loop import ScriptedModel, ScriptedToolProvider, _executing, text_response, tool_response


class UnknownResultProvider(ScriptedToolProvider):
    async def invoke(self, call, context):
        result = await super().invoke(call, context)
        if call.name == "fixture.fail":
            return replace(result, error_code="UNKNOWN_OUTCOME", message="response was lost",
                           recovery_kind=ToolRecoveryKind.UNKNOWN_OUTCOME)
        return result


def test_unknown_batch_result_does_not_cancel_other_calls_or_suspend_model():
    async def run():
        first = tool_response("batch", "unknown", "fixture.fail", {"text": "inspect"})
        first = replace(first, message=replace(first.message, content=first.message.content + (
            ToolCallBlock(ToolCall("other", "web.search", {"query": "independent observation"})),
        )))
        model = ScriptedModel([first, text_response("answer", "One result is unknown; the other query returned.")])
        provider = UnknownResultProvider()
        app = compose_fixture_application(model_adapter=model, tool_adapters=(provider,))
        with tempfile.TemporaryDirectory() as directory:
            await app.start()
            try:
                task = await _executing(app, Path(directory))
                result = await app.kernel.run_agent_turn(task.task_id, "Inspect both sources")
                assert isinstance(result, AgentTurnResult)
                assert len(model.requests) == 2
                results = {block.result.call_id: block.result for message in model.requests[1].messages
                           for block in message.content if isinstance(block, ToolResultBlock)}
                assert results["unknown"].error_code == "UNKNOWN_OUTCOME"
                assert results["other"].ok
                context = next(json.loads(message.text) for message in model.requests[1].messages
                               if message.message_id.startswith("execution-context-"))
                fact = next(item for item in context["recent_actions"] if item["call_id"] == "unknown")
                assert fact["operation_outcome"] == "unknown"
                assert fact["failure_code"] == "UNKNOWN_OUTCOME"
                live = await app.kernel.get_task(task.task_id)
                assert live.pending_clarification is None
                assert live.tool_executions[f"{result.turn_id}:unknown"].state is ToolCommitState.UNKNOWN_OUTCOME
                assert live.tool_executions[f"{result.turn_id}:other"].state is ToolCommitState.COMMITTED
                events = await app.kernel.dependencies.store.read_events(task.task_id)
                assert not any(e.event_type in {"clarification.requested", "outcome_reconciliation.requested"} for e in events)
            finally:
                await app.registry.stop_all()
    asyncio.run(run())
