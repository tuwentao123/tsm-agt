from __future__ import annotations

import time
import unittest
import asyncio
from unittest.mock import patch

from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.adapters.openai_compatible.request_diagnostics import RequestDiagnosticCapture, TurnRequestDiagnostics
from tsm_agt.ports import AdapterContext, Message, MessageRole, ModelRequest, TextBlock, ModelCallPurpose


class Transport:
    def __init__(self):
        self.payload = None

    async def post_json(self, url, headers, payload, timeout_seconds):
        self.payload = payload
        return {"id": "response", "choices": [{"message": {
            "role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {}}


class RequestDiagnosticTest(unittest.TestCase):
    def test_turn_capture_default_retention_is_one_hour(self):
        with patch('tsm_agt.adapters.openai_compatible.request_diagnostics.time.monotonic') as clock, \
             patch('tsm_agt.adapters.openai_compatible.request_diagnostics.threading.Timer') as timer:
            clock.return_value = 100.0
            capture = TurnRequestDiagnostics()
            key = capture.start('session')
            self.assertEqual(capture.read(key, 'session')['expires_in_seconds'], 3600)
            self.assertEqual(timer.call_args.args[0], 3600)
            clock.return_value = 401.0
            self.assertIsNotNone(capture.read(key, 'session'))
            clock.return_value = 3700.0
            self.assertIsNone(capture.read(key, 'session'))

    def test_opt_in_capture_is_bounded_redacted_and_one_shot(self):
        with self.assertRaises(ValueError):
            RequestDiagnosticCapture(acknowledge_sensitive_data=False)
        capture = RequestDiagnosticCapture(acknowledge_sensitive_data=True)
        provider = OpenAICompatibleModelProvider(
            "https://example.test", "model", "transport-secret", request_diagnostics=capture
        )
        request = ModelRequest("turn", (Message(
            "user", MessageRole.USER, (TextBlock("hello token=sample-secret"),)
        ),))
        payload, _ = provider._build_payload(request)
        provider._capture_request(request, payload)
        snapshot = capture.take()
        self.assertIsNotNone(snapshot)
        self.assertNotIn("sample-secret", str(snapshot))
        self.assertNotIn("transport-secret", str(snapshot))
        self.assertEqual(snapshot["provider_payload"]["messages"][0]["role"], "user")
        self.assertIn("sample-secret", str(payload))
        self.assertIsNone(capture.take())

    def test_expiration_and_size_limit(self):
        capture = RequestDiagnosticCapture(
            acknowledge_sensitive_data=True, ttl_seconds=0.03, max_bytes=1000
        )
        request = ModelRequest("turn", (Message("u", MessageRole.USER, (TextBlock("hello"),)),))
        capture.record(request, {"messages": []})
        time.sleep(0.08)
        self.assertIsNone(capture.take())
        capture.record(request, {"messages": ["x" * 2000]})
        self.assertIsNone(capture.take())


class RequestDiagnosticIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_opt_in_turn_capture_uses_real_openai_adapter_hook(self):
        capture = TurnRequestDiagnostics(ttl_seconds=10)
        transport = Transport()
        provider = OpenAICompatibleModelProvider(
            'https://example.test', 'model', 'secret', streaming=False,
            transport=transport, request_diagnostics=capture,
        )
        await provider.start(AdapterContext(config={}, emit_event=lambda *_: None))
        key = capture.start('session')
        token = capture.activate(key)
        try:
            request = ModelRequest('turn', (Message(
                'user', MessageRole.USER, (TextBlock('synthetic'),)),))
            await provider.complete(request)
        finally:
            capture.reset(token)
        result = capture.read(key, 'session')
        self.assertEqual(result['requests'][0]['provider_payload'], transport.payload)

    async def test_capture_matches_transmitted_payload_without_affecting_it(self):
        capture = RequestDiagnosticCapture(acknowledge_sensitive_data=True)
        transport = Transport()
        provider = OpenAICompatibleModelProvider(
            "https://example.test", "model", "secret", streaming=False,
            transport=transport, request_diagnostics=capture,
        )
        await provider.start(AdapterContext(config={}, emit_event=lambda *_: None))
        request = ModelRequest("turn", (Message(
            "user", MessageRole.USER, (TextBlock("synthetic request"),)
        ),))
        await provider.complete(request)
        snapshot = capture.take()
        self.assertEqual(snapshot["provider_payload"], transport.payload)
        self.assertEqual(snapshot["provider_neutral_messages"][0]["role"], "user")

    async def test_turn_capture_is_scoped_and_resume_survives_context_reset(self):
        capture = TurnRequestDiagnostics(ttl_seconds=1, max_calls=2)
        first = capture.start('session-a')
        second = capture.start('session-b')
        req_a = ModelRequest('turn-a', (Message('a', MessageRole.USER, (TextBlock('A'),)),))
        req_b = ModelRequest('turn-b', (Message('b', MessageRole.USER, (TextBlock('B'),)),))

        async def run(key, request):
            token = capture.activate(key)
            try:
                task = asyncio.create_task(asyncio.to_thread(capture.record, request, {'content': request.turn_id}))
                await task
            finally:
                capture.reset(token)

        await asyncio.gather(run(first, req_a), run(second, req_b))
        capture.record(req_a, {'content': 'after-approval'})
        capture.record(req_a, {'content': 'over-limit'})
        capture.record(ModelRequest('routing', (), purpose=ModelCallPurpose.SESSION_ROUTING), {})
        a = capture.read(first, 'session-a')
        b = capture.read(second, 'session-b')
        self.assertEqual([r['turn_id'] for r in a['requests']], ['turn-a', 'turn-a'])
        self.assertEqual(a['dropped'], 1)
        self.assertEqual([r['turn_id'] for r in b['requests']], ['turn-b'])
        self.assertIsNone(capture.read(first, 'session-b'))
        self.assertNotIn('turn-b', str(a))
        capture.delete(first)
        self.assertIsNone(capture.read(first, 'session-a'))

    async def test_turn_capture_default_off_and_expiration(self):
        capture = TurnRequestDiagnostics(ttl_seconds=0.05)
        request = ModelRequest('turn', ())
        capture.record(request, {'content': 'unselected'})
        key = capture.start('session')
        token = capture.activate(key)
        capture.record(request, {'content': 'selected token=secret'})
        capture.reset(token)
        self.assertNotIn('secret', str(capture.read(key, 'session')))
        await asyncio.sleep(0.1)
        self.assertIsNone(capture.read(key, 'session'))
