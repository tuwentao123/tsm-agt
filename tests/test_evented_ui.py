from __future__ import annotations

import asyncio
import importlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tsm_agt.adapters.builtin import CoreProcessToolProvider
from tsm_agt.adapters.local_skills import UserSkillsStore
from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.bootstrap import compose_fixture_application
from tsm_agt.core import ApprovalDecision
from tsm_agt.local_api import LocalEventApiServer
from tsm_agt.ports import RuntimeUnitOfWork, RuntimeEvent, SessionUnitOfWork, SessionTaskUnitOfWork, SessionEvent
from tsm_agt.sdk import EngineeringAgentClient
from tsm_agt.sdk.ui_events import subscribe_task_ui, ui_snapshot, subscribe_installation
from tests.test_conversation_loop import ScriptedModel, text_response, tool_response

web = importlib.import_module("tsm_agt.web.app")


class BlockingModel(ScriptedModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request):
        self.entered.set()
        await self.release.wait()
        return await super().complete(request)


class EventedUiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skills = UserSkillsStore(self.root / "home")

    async def client(self, model=None, database=None, tools=()):
        with patch("tsm_agt.bootstrap.composition.UserSkillsStore", return_value=self.skills):
            app = compose_fixture_application(model_adapter=model or ScriptedModel([]), tool_adapters=tools,
                                              store_adapter=SQLiteRuntimeStore(database) if database else None)
        client = EngineeringAgentClient(self.root, application_factory=lambda: app)
        await client.start()
        self.addAsyncCleanup(client.close)
        return client

    async def running(self):
        model = BlockingModel([text_response("done", "Finished")])
        client = await self.client(model)
        result = await client.submit_task("inspect", command_id="first")
        await model.entered.wait()
        self.addAsyncCleanup(self.release, model, client, result.task_id)
        return client, result.task_id

    async def release(self, model, client, task_id):
        model.release.set()
        await client.wait_task(task_id, timeout=5)

    async def test_commit_wakes_only_after_success_and_watchers_are_released(self):
        client, task_id = await self.running()
        store = client._store()
        with store.watch(task_id) as signal:
            wake = signal.changed
            await client.application.kernel._append_events(task_id, (("test.committed", {}),))
            self.assertTrue(wake.is_set())
            next_wake = signal.changed
            task = await store.load_task(task_id)
            with self.assertRaises(ValueError):
                await store.commit(RuntimeUnitOfWork(task_id, task.version - 1, task.data, ()))
            self.assertFalse(next_wake.is_set())
        self.assertNotIn(("task", task_id), store._signals)

    async def test_atomic_session_task_commit_notifies_both_only_after_success(self):
        for database in (None, self.root / "atomic.db"):
            client = await self.client(database=database)
            session = await client.application.kernel.create_session("atomic")
            task = await client.create_task("atomic", command_id="atomic-create", session_id=session.session_id)
            store = client._store()
            session_before, task_before = await store.load_session(session.session_id), await store.load_task(task.task_id)
            session_unit = SessionUnitOfWork(session.session_id, session_before.version, session_before.data, (
                SessionEvent("atomic-session", session.session_id, session_before.last_event_sequence + 1, "test.session"),
            ))
            task_unit = RuntimeUnitOfWork(task.task_id, task_before.version - 1, task_before.data, ())
            with store.watch(task.task_id) as task_signal, store.watch(session.session_id, scope="session") as session_signal:
                task_wake, session_wake = task_signal.changed, session_signal.changed
                with self.assertRaises(ValueError):
                    await store.commit_session_and_task(SessionTaskUnitOfWork(session_unit, task_unit, "atomic-fail"))
                self.assertFalse(task_wake.is_set())
                self.assertFalse(session_wake.is_set())
                self.assertEqual((await store.load_session(session.session_id)).version, session_before.version)
                task_unit = RuntimeUnitOfWork(task.task_id, task_before.version, task_before.data, (
                    RuntimeEvent("atomic-task", task.task_id, task_before.last_event_sequence + 1, "test.task"),
                ))
                await store.commit_session_and_task(SessionTaskUnitOfWork(session_unit, task_unit, "atomic-ok"))
                self.assertTrue(task_wake.is_set())
                self.assertTrue(session_wake.is_set())
            await client.close()

    async def test_sqlite_event_reads_are_bounded_and_resume_without_gaps(self):
        client = await self.client(database=self.root / "bounded.db")
        task = await client.create_task("bounded", command_id="bounded-create")
        await client.application.kernel._append_events(task.task_id, tuple(("test.event", {"index": index}) for index in range(7)))
        store = client._store()
        cursor, sequences = 0, []
        while page := await store.read_events(task.task_id, cursor, limit=3):
            self.assertLessEqual(len(page), 3)
            sequences.extend(event.sequence for event in page)
            cursor = page[-1].sequence
        all_events = await store.read_events(task.task_id)
        self.assertEqual(sequences, [event.sequence for event in all_events])

    async def test_subscribe_events_has_no_lost_wake_between_read_and_wait(self):
        client, task_id = await self.running()
        store = client._store()
        cursor = (await store.load_task(task_id)).last_event_sequence
        original = store.read_events
        committed = False

        async def racing_read(*args, **kwargs):
            nonlocal committed
            result = await original(*args, **kwargs)
            if not result and not committed:
                committed = True
                await client.application.kernel._append_events(task_id, (("test.raced", {}),))
            return result

        stream = client.subscribe_events(task_id, after=cursor, timeout=1)
        with patch.object(store, "read_events", side_effect=racing_read):
            event = await asyncio.wait_for(anext(stream), 1)
            self.assertEqual(event.event_type, "test.raced")
        await stream.aclose()

    async def test_idle_ui_heartbeats_never_read_database_or_recompute_projection(self):
        client, task_id = await self.running()
        store = client._store()
        stream = subscribe_task_ui(client, task_id, heartbeat=0.01)
        with patch.object(store, "read_events", wraps=store.read_events) as reads, patch.object(client, "get_task_result", wraps=client.get_task_result) as snapshots:
            async for packet in stream:
                if packet["type"] == "task.snapshot":
                    break
            before = (reads.call_count, snapshots.call_count)
            for _ in range(3):
                packet = await asyncio.wait_for(anext(stream), 1)
                self.assertEqual(packet["type"], "stream.heartbeat")
            self.assertEqual((reads.call_count, snapshots.call_count), before)
        await stream.aclose()
        self.assertFalse(store._signals)

    async def test_open_ui_stream_delivers_later_commits_and_final_answer(self):
        model = BlockingModel([text_response("done", "Live final answer")])
        client = await self.client(model, self.root / "live-stream.db")
        result = await client.submit_task("inspect", command_id="live-stream")
        await model.entered.wait()
        stream = subscribe_task_ui(client, result.task_id, heartbeat=0.01)
        self.addAsyncCleanup(stream.aclose)
        packets = []
        async for packet in stream:
            packets.append(packet)
            if packet["type"] == "task.snapshot":
                break
        self.assertEqual(packets[-1]["status"], "running")
        await client.application.kernel._append_events(result.task_id, ((
            "conversation.execution_note", {"turn_id": packets[-1]["turn_id"], "text": "Still working"},
        ),))
        packet = await asyncio.wait_for(anext(stream), 1)
        self.assertEqual(packet.get("progress", {}).get("activity"), "Still working")
        model.release.set()
        async def drain():
            return [packet async for packet in stream]
        rest = await asyncio.wait_for(drain(), 5)
        self.assertEqual(rest[-1]["assistant_text"], "Live final answer")
        self.assertEqual(rest[-1]["status"], "awaiting_input")

    async def test_durable_replay_and_final_message_survive_sqlite_restart(self):
        database = self.root / "runtime.db"
        client = await self.client(ScriptedModel([text_response("done", "Persisted answer")]), database)
        result = await client.submit_task("inspect", command_id="persist")
        await client.wait_task(result.task_id, timeout=5)
        stored = await client._store().load_task(result.task_id)
        await client.close()
        restarted = await self.client(database=database)
        packets = [packet async for packet in restarted.subscribe_task_ui(result.task_id, after=stored.last_event_sequence - 2)]
        sequences = [packet["sequence"] for packet in packets if packet.get("progress")]
        self.assertTrue(all(sequence > stored.last_event_sequence - 2 for sequence in sequences))
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertEqual(packets[-1]["assistant_text"], "Persisted answer")
        self.assertEqual(packets[-1]["status"], "awaiting_input")
        self.assertTrue(packets[-1]["active_turn_id"])
        self.assertFalse(restarted._store()._signals)

    async def test_future_cursor_explicitly_resets_without_mutating_task(self):
        client = await self.client(ScriptedModel([text_response("done", "Done")]))
        result = await client.submit_task("inspect", command_id="reset")
        await client.wait_task(result.task_id, timeout=5)
        before = await client._store().load_task(result.task_id)
        packets = [packet async for packet in client.subscribe_task_ui(result.task_id, after=before.last_event_sequence + 100)]
        self.assertEqual(packets[0]["type"], "stream.reset_required")
        self.assertEqual(packets[-1]["cursor"], before.last_event_sequence)
        self.assertEqual((await client._store().load_task(result.task_id)).version, before.version)

    async def test_snapshot_never_acknowledges_a_concurrent_undelivered_event(self):
        client, task_id = await self.running()
        module = importlib.import_module("tsm_agt.sdk.ui_events")
        original = module.ui_snapshot
        calls = 0
        async def concurrent_snapshot(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                task = await client.application.kernel.get_task(task_id)
                await client.application.kernel._append_events(task_id, (("conversation.execution_note", {
                    "turn_id": task.active_agent_checkpoint["turn_id"], "text": "concurrent change",
                }),))
            return await original(*args, **kwargs)
        stream = client.subscribe_task_ui(task_id)
        packets = []
        with patch.object(module, "ui_snapshot", side_effect=concurrent_snapshot):
            async for packet in stream:
                packets.append(packet)
                if packet["type"] == "task.snapshot":
                    break
        concurrent = [packet for packet in packets if packet.get("progress", {}).get("activity") == "concurrent change"]
        self.assertEqual(len(concurrent), 1)
        self.assertLessEqual(concurrent[0]["sequence"], packets[-1]["cursor"])
        await stream.aclose()

    async def test_prior_turn_answer_is_absent_while_next_turn_is_running(self):
        model = ScriptedModel([text_response("one", "First answer")])
        client = await self.client(model)
        result = await client.submit_task("first", command_id="turn-one")
        await client.wait_task(result.task_id, timeout=5)
        first = await ui_snapshot(client, result.task_id)
        self.assertEqual(first["assistant_text"], "First answer")
        blocker = BlockingModel([text_response("two", "Second answer")])
        with patch.object(model, "complete", side_effect=blocker.complete):
            await client._start_task_turn(result.task_id, "second")
            await blocker.entered.wait()
            second = await ui_snapshot(client, result.task_id)
            self.assertNotEqual(second["turn_id"], first["turn_id"])
            self.assertIsNone(second["assistant_text"])
            self.assertGreater(second["active_turn_sequence"], first["active_turn_sequence"])
            blocker.release.set()
            await client.wait_task(result.task_id, timeout=5)

    async def test_approval_is_pushed_and_no_raw_tool_arguments_leak(self):
        client = await self.client(ScriptedModel([
            tool_response("check", "check-call", "core.run_command", {"argv": ["node", "--version"], "cwd": "."}),
        ]), tools=(CoreProcessToolProvider(),))
        result = await client.submit_task("check", command_id="approval")
        await client.wait_task(result.task_id, timeout=5)
        packets = [packet async for packet in client.subscribe_task_ui(result.task_id)]
        state = packets[-1]
        self.assertEqual(state["waiting"]["kind"], "APPROVAL")
        self.assertEqual(state["status"], "awaiting_approval")
        self.assertTrue(state["approval"]["request_id"])
        tools = [packet for packet in packets if packet["type"] == "tool.requested"]
        self.assertTrue(tools)
        self.assertNotIn("argv", tools[0]["progress"])

    async def test_rejected_approval_resumes_same_task_and_pushes_final_answer(self):
        client = await self.client(ScriptedModel([
            tool_response("check", "check-call", "core.run_command", {"argv": ["node", "--version"], "cwd": "."}),
            text_response("declined", "用户拒绝，命令未执行。"),
        ]), tools=(CoreProcessToolProvider(),))
        result = await client.submit_task("check", command_id="approval-reject")
        waiting = await client.wait_task(result.task_id, timeout=5)
        first = [packet async for packet in client.subscribe_task_ui(result.task_id)]
        self.assertEqual(first[-1]["status"], "awaiting_approval")
        await client.resolve_approval(waiting.approval["request_id"], ApprovalDecision.DENY, "test declined", command_id="reject-once")
        await client.wait_task(result.task_id, timeout=5)
        second = [packet async for packet in client.subscribe_task_ui(result.task_id, after=first[-1]["cursor"])]
        self.assertEqual(second[-1]["assistant_text"], "用户拒绝，命令未执行。")
        self.assertEqual(second[-1]["status"], "awaiting_input")
        self.assertTrue(any(packet["type"] == "approval.resolved" for packet in second))

    async def test_cancellation_pushes_terminal_state_and_closes_subscriber(self):
        client, task_id = await self.running()
        await client.cancel(task_id, command_id="cancel-once", reason="test cancel")
        packets = [packet async for packet in client.subscribe_task_ui(task_id)]
        self.assertEqual(packets[-1]["phase1_state"], "CANCELLED")
        self.assertTrue(packets[-1]["final"])
        self.assertFalse(client._store()._signals)

    async def test_reviewer_failure_and_partial_boundary_are_visible_live_and_after_reload(self):
        client, task_id = await self.running()
        snapshot = await ui_snapshot(client, task_id)
        turn_id = snapshot["turn_id"]
        await client.application.kernel._append_events(task_id, (
            ("turn.completion_review_started", {"turn_id": turn_id}),
            ("turn.completion_review_failed", {"turn_id": turn_id, "error_type": "CompletionReviewError"}),
            ("turn.incomplete_boundary", {"turn_id": turn_id, "reason": "completion_reviewer_unavailable"}),
        ))
        packets = [packet async for packet in client.subscribe_task_ui(task_id, after=snapshot["cursor"])]
        progress = [packet["progress"] for packet in packets if packet.get("progress")]
        self.assertEqual([p.get("review_action") for p in progress if p["kind"] == "completion_review"], ["STARTED", "FAILED"])
        self.assertEqual(progress[-1]["kind"], "turn_interrupted")
        final = packets[-1]
        self.assertTrue(final["final"])
        self.assertEqual(final["status"], "incomplete")
        self.assertIsNone(final["assistant_text"])
        self.assertIn("完成复核失败", final["failure_reason"])
        module = importlib.import_module("tsm_agt.sdk.ui_events")
        history = [module._turn_progress_item(event) for event in await client._store().read_events(
            task_id, event_types=module._TURN_PROGRESS_EVENT_TYPES)]
        self.assertTrue(any(item and item.get("diagnostic_code") == "completion_reviewer_unavailable" for item in history))

    async def test_slow_subscriber_does_not_block_commit_or_other_subscribers(self):
        client, task_id = await self.running()
        slow = client.subscribe_task_ui(task_id)
        await anext(slow)
        other = client.subscribe_events(task_id, after=(await client._store().load_task(task_id)).last_event_sequence, timeout=1)
        pending = asyncio.create_task(anext(other))
        await asyncio.sleep(0)
        await client.application.kernel._append_events(task_id, (("test.other", {}),))
        self.assertEqual((await asyncio.wait_for(pending, 1)).event_type, "test.other")
        await slow.aclose()
        await other.aclose()
        self.assertFalse(client._store()._signals)

    async def test_installation_observer_waits_for_dispatch_without_polling(self):
        client = await self.client()
        skill = self.skills.install_upload({"SKILL.md": b"---\nname: demo\ndescription: Demo\n---\nNo dependencies."})
        job = self.skills.installations.create(skill)
        job = self.skills.installations.update(job["id"], status="starting", session_id="install-chat")
        stream = subscribe_installation(client, self.skills.installations, job["id"], heartbeat=0.01)
        self.assertEqual((await anext(stream))["installation"]["status"], "starting")
        self.assertEqual((await anext(stream))["type"], "stream.heartbeat")
        self.skills.installations.update(job["id"], status="pending", summary="dispatch failed")
        client._store().notify("install-chat", scope="session")
        packet = await asyncio.wait_for(anext(stream), 1)
        self.assertEqual(packet["installation"]["status"], "pending")
        self.assertFalse(packet["watching"])
        await stream.aclose()


class RuntimeLoopBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_reads_and_closes_on_runtime_loop_without_blocking_web_loop(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever)
        thread.start()
        server = LocalEventApiServer.__new__(LocalEventApiServer)
        server._loop = loop
        entered, closed = threading.Event(), threading.Event()
        owners = []

        async def source():
            try:
                owners.append(threading.get_ident())
                entered.set()
                await asyncio.Event().wait()
                yield {}
            finally:
                owners.append(threading.get_ident())
                closed.set()

        stream = server.iterate_runtime(source())
        try:
            waiting = asyncio.create_task(anext(stream))
            await asyncio.to_thread(entered.wait, 1)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertTrue(await asyncio.to_thread(closed.wait, 1))
            self.assertEqual(owners, [thread.ident, thread.ident])
        finally:
            loop.call_soon_threadsafe(loop.stop)
            await asyncio.to_thread(thread.join, 2)
            loop.close()
