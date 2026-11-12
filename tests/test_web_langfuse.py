"""Web opt-in and metadata-only Langfuse export through the normal chat route."""

import asyncio
import json
from types import SimpleNamespace

import httpx

from tests.runtime_fixtures import RuntimeEchoModel
from tsm_agt.adapters.evented_runtime_store import EventedRuntimeStore
from tsm_agt.adapters.fixture.runtime_store import InMemoryRuntimeStore
from tsm_agt.adapters.langfuse import LangfuseClient
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.local_api import LocalEventApiServer
from tsm_agt.ports import RuntimeEvent, RuntimeUnitOfWork
from tsm_agt.web import app as web
from tsm_agt.web import langfuse


def test_web_langfuse_records_only_opted_in_future_turns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web, "web_langfuse", langfuse.WebLangfuse())
    monkeypatch.setattr(web, "runtime_server", None)
    monkeypatch.setattr(web, "runtime_error", None)
    monkeypatch.setattr(web, "configuration_issues", [])
    monkeypatch.setattr(web, "workspace_registry", [])
    monkeypatch.setattr(web, "session_workspace_bindings", {})
    monkeypatch.setattr(web, "session_auto_approvals", {})
    monkeypatch.setattr(web, "removed_workspace_paths", set())
    monkeypatch.setattr(web, "removed_session_ids", set())
    config_path = tmp_path / "langfuse.yaml"
    monkeypatch.setattr(langfuse, "langfuse_config_path", lambda: config_path)
    monkeypatch.setattr(
        "tsm_agt.bootstrap.composition.UserSkillsStore",
        lambda: UserSkillsStore(tmp_path / "skills"),
    )
    application = compose_fixture_application(
        model_adapter=RuntimeEchoModel(), tool_adapters=(),
    )
    server = LocalEventApiServer(tmp_path, port=0, application_factory=lambda: application)
    # ASGITransport drives Web routes in-process. The SDK's separate HTTP
    # listener is unused, while its Runtime thread still executes the turns.
    monkeypatch.setattr("tsm_agt.local_api.ThreadingHTTPServer", lambda address, _: SimpleNamespace(
        server_address=address, server_close=lambda: None, shutdown=lambda: None,
    ))
    monkeypatch.setattr(web, "LocalEventApiServer", lambda root: server)
    sent = []

    def capture_request(client, path, payload):
        sent.append((client.base_url, path, payload))
        return {}

    monkeypatch.setattr(LangfuseClient, "request", capture_request)

    async def wait_for_exports():
        await asyncio.gather(*web.web_langfuse._pending)

    async def exercise():
        await web.startup()
        try:
            assert web.runtime_server is server, web.runtime_error
            transport = httpx.ASGITransport(app=web.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                registered = await client.post("/workspaces", json={"path": str(tmp_path)})
                assert registered.status_code == 200, registered.text
                workspace = registered.json()["id"]

                async def submit(session_id, request_id):
                    response = await client.post("/session-input", json={
                        "session_id": session_id,
                        "request_id": request_id,
                        "workspace_id": workspace,
                        "text": "Hello PRIVATE-PROMPT " + request_id,
                    })
                    assert response.status_code in {200, 202}, response.text
                    data = response.json()
                    server._call(server._client.wait_task(data["task"]["task_id"], timeout=5))
                    server._call(wait_for_exports())

                await submit("session-private-id", "before")
                switch_url = "/sessions/session-private-id/langfuse"
                response = await client.post(switch_url, json={"enabled": True})
                assert response.status_code == 400
                assert "langfuse.yaml" in response.json()["detail"]
                config_path.write_text(
                    "base_url: https://langfuse.example\npublic_key: sample\nsecret_key: PRIVATE-KEY\n",
                    encoding="utf-8",
                )
                blocked = await client.post(switch_url, json={"enabled": True}, headers={
                    "Origin": "https://another.example",
                })
                assert blocked.status_code == 403
                response = await client.post(switch_url, json={"enabled": True})
                assert response.json() == {"session_id": "session-private-id", "enabled": True}
                await submit("session-private-id", "first")
                await submit("another-session", "default-off")
                await submit("session-private-id", "second")
                listed = await client.get("/sessions")
                assert {entry["session_id"]: entry["langfuse_enabled"]
                        for entry in listed.json()["sessions"]} == {
                    "session-private-id": True, "another-session": False,
                }
                response = await client.post(switch_url, json={"enabled": False})
                assert response.json()["enabled"] is False
                await submit("session-private-id", "after")
        finally:
            await web.shutdown()

    asyncio.run(exercise())
    assert len(sent) == 2
    traces = set()
    sessions = set()
    for base_url, path, payload in sent:
        assert base_url == "https://langfuse.example"
        assert path == "/api/public/otel/v1/traces"
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert [span["name"] for span in spans] == [
            "tsm-agt conversation turn", "Model response 1",
        ]
        assert spans[1]["parentSpanId"] == spans[0]["spanId"]
        assert int(spans[0]["endTimeUnixNano"]) >= int(spans[0]["startTimeUnixNano"])
        attributes = [{item["key"]: item["value"]["stringValue"]
                       for item in span["attributes"]} for span in spans]
        assert attributes[0]["langfuse.observation.metadata.status"] == "completed"
        assert json.loads(attributes[0]["langfuse.observation.output"]) == {
            "status": "completed", "model_responses": 1, "tool_calls": 0, "tool_failures": 0,
        }
        usage = json.loads(attributes[1]["langfuse.observation.usage_details"])
        assert json.loads(attributes[1]["langfuse.observation.input"]) == {
            "model_call": 1, "input_tokens": usage["input"],
        }
        assert json.loads(attributes[1]["langfuse.observation.output"]) == {
            "finish_reason": "stop", "output_tokens": usage["output"], "tool_calls_requested": 0,
        }
        traces.add(spans[0]["traceId"])
        sessions.update(attrs["langfuse.session.id"] for attrs in attributes)
        raw = json.dumps(payload)
        for private in ("PRIVATE-PROMPT", "PRIVATE-KEY", "session-private-id", str(tmp_path)):
            assert private not in raw
    assert len(traces) == 2
    assert len(sessions) == 1


def test_langfuse_turn_interruption_and_tool_failure_stay_metadata_only(monkeypatch):
    client = LangfuseClient("http://127.0.0.1", "sample", "sample-secret")
    monkeypatch.setattr(LangfuseClient, "from_config", lambda _: client)
    recorder = langfuse.WebLangfuse()
    store = EventedRuntimeStore(InMemoryRuntimeStore())
    store.on_task_commit = lambda unit: recorder.observe_commit(store, unit)
    sent = []
    monkeypatch.setattr(client, "send_trace", sent.append)

    async def exercise():
        async def commit(sequence, turn_id, event_type, **payload):
            event = RuntimeEvent(
                f"event-{sequence}", "task-private", sequence, event_type,
                {"turn_id": turn_id, **payload},
            )
            await store.commit(RuntimeUnitOfWork(
                "task-private", sequence - 1, {"session_id": "private-session"}, (event,),
            ))

        await recorder.configure("private-session", True)
        await commit(1, "first-turn", "turn.started")
        await recorder.configure("private-session", False)
        await recorder.configure("private-session", True)
        await commit(2, "first-turn", "turn.completed")

        await commit(3, "second-turn", "turn.started")
        await commit(4, "second-turn", "tool.started", execution_id="private-execution", tool={
            "name": "private.tool", "effect": "execute",
        }, call={"arguments": {"command": "PRIVATE-COMMAND"}})
        await commit(5, "second-turn", "tool.failed", execution_id="private-execution",
                     commit_state="FAILED", result={
                         "message": "PRIVATE-TOOL-ERROR",
                         "error_code": "PRIVATE_CODE",
                         "data": {"output": "PRIVATE-CODE"},
                         "recovery_kind": "retry_same",
                         "retryable": True,
                         "truncated": True,
                     })
        await commit(6, "second-turn", "turn.interrupted", reason="PRIVATE-ERROR")
        await asyncio.gather(*recorder._pending)
        await recorder.close()

    asyncio.run(exercise())
    assert len(sent) == 1
    raw = json.dumps(sent[0])
    for private in ("PRIVATE-", "PRIVATE_CODE", "private.tool", "private-execution", "second-turn"):
        assert private not in raw
    spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert [span["name"] for span in spans] == ["tsm-agt conversation turn", "Tool call · execute"]
    attrs = [{item["key"]: item["value"]["stringValue"] for item in span["attributes"]}
             for span in spans]
    assert attrs[0]["langfuse.observation.metadata.status"] == "interrupted"
    assert attrs[1]["langfuse.observation.metadata.status"] == "failed"
    assert attrs[1]["langfuse.observation.metadata.effect"] == "execute"
    assert json.loads(attrs[0]["langfuse.observation.output"]) == {
        "status": "interrupted", "model_responses": 0, "tool_calls": 1, "tool_failures": 1,
    }
    assert json.loads(attrs[1]["langfuse.observation.input"]) == {"category": "execute"}
    assert json.loads(attrs[1]["langfuse.observation.output"]) == {
        "status": "failed", "commit_state": "FAILED", "recovery_kind": "retry_same",
        "retryable": True, "result_truncated": True,
    }
