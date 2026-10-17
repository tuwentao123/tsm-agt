from __future__ import annotations

import time
import unittest

from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.adapters.openai_compatible.request_diagnostics import RequestDiagnosticCapture
from tsm_agt.ports import AdapterContext, Message, MessageRole, ModelRequest, TextBlock


class Transport:
    def __init__(self):
        self.payload = None

    async def post_json(self, url, headers, payload, timeout_seconds):
        self.payload = payload
        return {"id": "response", "choices": [{"message": {
            "role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {}}


class RequestDiagnosticTest(unittest.TestCase):
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
