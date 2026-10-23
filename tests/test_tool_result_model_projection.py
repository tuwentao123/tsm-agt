from __future__ import annotations

import json
import unittest

from tsm_agt.adapters.anthropic_messages import AnthropicMessagesModelProvider
from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.core.context import ContextWindowManager
from tsm_agt.core.kernel_checkpoint import CheckpointMixin
from tsm_agt.ports import Message, MessageRole, ToolCall, ToolCallBlock, ToolRecoveryKind, ToolResult, ToolResultBlock


class ToolResultModelProjectionTest(unittest.TestCase):
    def test_internal_retry_classification_stays_durable_but_not_in_provider_messages(self):
        result = ToolResult(
            'call', False, error_code='CONFLICT', message='file changed', hint='Read the current file.',
            recovery_kind=ToolRecoveryKind.RETRY_AFTER_STATE_CHANGE,
            recovery_action={'required_change': 'reread', 'same_call_safe': False,
                             'resource': 'a.txt', 'actual_hash': 'abc'},
        )
        self.assertEqual(result.to_data()['recovery_kind'], 'retry_after_state_change')
        self.assertEqual(ToolResult.from_data(result.to_data()), result)
        msg = Message('result', MessageRole.TOOL, (ToolResultBlock(result),))
        openai = OpenAICompatibleModelProvider._message_to_provider(msg, {})
        anthropic = AnthropicMessagesModelProvider(
            'https://example.test', 'model', 'key',
        )._message_to_provider(msg, {}, frozenset())
        for body in (openai['content'], anthropic['content'][0]['content']):
            data = json.loads(body)
            self.assertEqual(data['message'], 'file changed')
            self.assertEqual(data['hint'], 'Read the current file.')
            self.assertEqual(data['error_details'], {'resource': 'a.txt', 'actual_hash': 'abc'})
            self.assertNotIn('retryable', data)
            self.assertNotIn('recovery_kind', data)
            self.assertNotIn('recovery_action', data)
        self.assertTrue(anthropic['content'][0]['is_error'])

    def test_compacted_summary_and_continuation_do_not_instruct_retry(self):
        result = ToolResult('call', False, error_code='BAD', message='invalid argument',
                            recovery_kind=ToolRecoveryKind.RETRY_WITH_CORRECTED_ARGUMENTS)
        summary = ContextWindowManager._tool_result_summary(result, None)
        self.assertEqual(summary['message'], 'invalid argument')
        self.assertNotIn('recovery_kind', summary)
        messages = (
            Message('call-msg', MessageRole.ASSISTANT, (ToolCallBlock(ToolCall('call', 'tool', {})),)),
            Message('result-msg', MessageRole.TOOL, (ToolResultBlock(result),)),
        )
        retained = CheckpointMixin._compact_continuation_messages(messages, 'continue', 'input')
        self.assertEqual(retained[:2], messages)
        self.assertNotIn('call the same tool', retained[-1].text)
        self.assertNotIn('retryable', retained[-1].text)


if __name__ == '__main__':
    unittest.main()
