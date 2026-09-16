from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tsm_agt.adapters.fixture import EchoModelProvider
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.local_api import LocalEventApiServer
from tsm_agt.core import TaskState


class LocalEventApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        application = compose_fixture_application(
            model_adapter=EchoModelProvider(), tool_adapters=()
        )
        self.server = LocalEventApiServer(
            self.root, port=0, token="fixture-local-api-token-123456",
            application_factory=lambda: application,
        )
        self.server.start()
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        host, port = self.server.address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, path: str, body=None, *, authorized=True):
        headers = {}
        if authorized:
            headers["Authorization"] = "Bearer fixture-local-api-token-123456"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = Request(self.base + path, data=data, headers=headers)
        with urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read().decode()

    def test_auth_task_lifecycle_and_sse_cursor_resume(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.request("/v1/health", authorized=False)
        self.assertEqual(caught.exception.code, 401)

        status, _, raw = self.request("/v1/tasks", {
            "goal": "hello API", "command_id": "api-create-1"
        })
        self.assertEqual(status, 202)
        task_id = json.loads(raw)["task_id"]

        for _ in range(100):
            _, _, raw = self.request(f"/v1/tasks/{task_id}")
            task = json.loads(raw)
            if task["state"] == "SUCCEEDED":
                break
        self.assertEqual(task["assistant_text"], "hello API")

        _, headers, raw = self.request(
            f"/v1/tasks/{task_id}/events?after=0"
        )
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        data_lines = [
            json.loads(line[6:]) for line in raw.splitlines()
            if line.startswith("data: ")
        ]
        events = [item for item in data_lines if "event_id" in item]
        self.assertEqual(
            [item["cursor"] for item in events],
            list(range(1, task["cursor"] + 1)),
        )
        self.assertTrue(all("payload" not in item for item in events))
        _, _, resumed_raw = self.request(
            f"/v1/tasks/{task_id}/events?after={task['cursor']}"
        )
        self.assertNotIn("event_id", resumed_raw)

        _, progress_headers, progress_raw = self.request(
            f"/v1/tasks/{task_id}/progress?after=0"
        )
        self.assertTrue(
            progress_headers["Content-Type"].startswith("text/event-stream")
        )
        progress_data = [
            json.loads(line[6:]) for line in progress_raw.splitlines()
            if line.startswith("data: ")
        ]
        live = [item for item in progress_data if "progress" in item]
        self.assertTrue(live)
        self.assertTrue(all(
            item["progress"]["goal"] == "hello API" for item in live
        ))
        cursor = progress_data[-1]
        self.assertEqual(cursor["persistence"], "ephemeral")
        self.assertEqual(cursor["redaction"], "none-local-authenticated-ui")

    def test_loopback_and_token_are_mandatory(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            LocalEventApiServer(self.root, host="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "24 characters"):
            LocalEventApiServer(self.root, token="short")

    def test_runtime_steer_and_replace_routes(self) -> None:
        task = self.server._call(self.server._client.create_task(
            "steering API", command_id="api-steering-create"
        ))
        for state in (
            TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            self.server._call(self.server._client.application.kernel.transition_task(
                task.task_id, state, state.value
            ))
        status, _, raw = self.request(f"/v1/tasks/{task.task_id}/steer", {
            "text": "keep output short", "command_id": "api-steer-1"
        })
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(raw)["result"]["kind"], "steer")
        status, _, raw = self.request(f"/v1/tasks/{task.task_id}/replace", {
            "text": "new goal", "command_id": "api-replace-1"
        })
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(raw)["result"]["kind"], "replace")
        status, _, raw = self.request(f"/v1/tasks/{task.task_id}/input", {
            "text": "另外补上单元测试", "command_id": "api-input-1",
            "intent": "steer",
        })
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(raw)["result"]["intent"], "STEER")
        status, _, raw = self.request(f"/v1/tasks/{task.task_id}/input", {
            "text": "arbitrary payload 42", "command_id": "api-input-2"
        })
        self.assertEqual(status, 202)
        default_route = json.loads(raw)["result"]
        self.assertEqual(default_route["intent"], "STEER")
        self.assertFalse(default_route["requires_confirmation"])

    def test_task_spec_get_and_revision_route(self) -> None:
        task = self.server._call(self.server._client.create_task(
            "API Task SPEC", command_id="api-spec-create"
        ))
        status, _, raw = self.request(f"/v1/tasks/{task.task_id}/spec")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["goal"], "API Task SPEC")
        status, _, raw = self.request(f"/v1/tasks/{task.task_id}/spec", {
            "expected_revision": 1, "scope": ["src"],
            "constraints": ["no API changes"],
            "acceptance_criteria": [{
                "criterion_id": "integrity",
                "description": "workspace remains consistent",
                "verification_kind": "workspace_integrity",
            }],
            "command_id": "api-spec-update",
        })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["result"]["revision"], 2)


if __name__ == "__main__":
    unittest.main()
