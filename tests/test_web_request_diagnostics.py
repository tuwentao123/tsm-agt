from __future__ import annotations

import importlib
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from tsm_agt.adapters.openai_compatible.request_diagnostics import TurnRequestDiagnostics
from tsm_agt.ports import Message, MessageRole, ModelRequest, TextBlock


def request(*, host='127.0.0.1', peer='127.0.0.1', origin=None):
    headers = [(b'host', f'{host}:8000'.encode())]
    if origin:
        headers.append((b'origin', origin.encode()))
    return Request({'type': 'http', 'method': 'POST', 'scheme': 'http',
                    'path': '/', 'query_string': b'', 'headers': headers,
                    'server': (host, 8000), 'client': (peer, 1234)})


class WebRequestDiagnosticsTest(unittest.IsolatedAsyncioTestCase):
    async def test_opt_in_input_captures_and_requires_capability(self):
        web = importlib.import_module('tsm_agt.web.app')
        capture = TurnRequestDiagnostics(ttl_seconds=10)

        class Kernel:
            async def get_session(self, _session_id):
                return object()

        class Client:
            application = type('Application', (), {'kernel': Kernel()})()

            async def submit_user_input(self, *args, **kwargs):
                model_request = ModelRequest('turn-web', (Message(
                    'u', MessageRole.USER, (TextBlock('synthetic'),)),))
                capture.record(model_request, {'messages': [{'content': 'synthetic'}]})
                return type('Result', (), {'result': {'kind': 'task'}})()

        class Server:
            _client = Client()

            def _call(self, coroutine):
                try:
                    coroutine.send(None)
                except StopIteration as done:
                    return done.value
                raise AssertionError('unexpected suspension')

        with patch.object(web, 'runtime_server', Server()), \
             patch.object(web, 'turn_request_diagnostics', capture), \
             patch.object(web, 'workspace_registry', [{'id': 'w', 'path': '/tmp'}]), \
             patch.object(web, 'session_workspace_bindings', {}), \
             patch.object(web, 'save_session_workspace_bindings'):
            response = await web.submit_session_input({
                'workspace_id': 'w', 'session_id': 's', 'request_id': 'r',
                'text': 'synthetic', 'diagnose_request': True,
            }, request(origin='http://127.0.0.1:8000'))
            key = json.loads(response.body)['diagnostic_key']
            result = await web.read_turn_diagnostics({'key': key, 'session_id': 's'}, request())
            data = json.loads(result.body)
            self.assertEqual(data['turn_id'], 'turn-web')
            self.assertEqual(len(data['requests']), 1)
            self.assertEqual(result.headers['cache-control'], 'no-store')
            with self.assertRaises(HTTPException):
                await web.read_turn_diagnostics({'key': key, 'session_id': 'other'}, request())
            with self.assertRaises(HTTPException):
                await web.read_turn_diagnostics({'key': key, 'session_id': 's'}, request(origin='http://evil.test'))
            with self.assertRaises(HTTPException):
                await web.read_turn_diagnostics({'key': key, 'session_id': 's'}, request(peer='192.0.2.1'))


if __name__ == '__main__':
    unittest.main()
