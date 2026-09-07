from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from tsm_agt.adapters.sqlite import SQLiteRuntimeStore
from tsm_agt.adapters.fixture import ToolCallingModelProvider
from tsm_agt.bootstrap import (
    compose_fixture_application, compose_local_flow_query_application,
)
from tsm_agt.cli import (
    _flow, _flow_query, _play_replay, _replay,
    render_flow_node_diagnostic, render_flow_timeline, render_flow_tree,
    render_flow_tui,
)
from tsm_agt.core import TaskNotFound, TaskState
from tsm_agt.core import (
    FlowNodeKind, FlowNodeStatus, FlowReplay, FlowReplayBoundary, FlowReplaySpeed,
)
from tsm_agt.ports import (
    AdapterContext, ReplayCursorStorePort, RuntimeStorePort, ToolCall,
)


class FlowCliTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.database = self.workspace / ".agent" / "runtime.db"
        self.application = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(self.database)
        )
        await self.application.registry.start_all()
        task = await self.application.kernel.create_task(
            "private goal must not be rendered",
            self.workspace,
            "task-cli-flow",
        )
        for state in (
            TaskState.INTAKE,
            TaskState.RESOLVING_PROJECT,
            TaskState.SELECTING_EXTENSIONS,
            TaskState.ROUTING,
            TaskState.EXECUTING,
        ):
            task = await self.application.kernel.transition_task(
                task.task_id, state, f"enter {state.value}"
            )
        result = await self.application.kernel.invoke_tool(
            task.task_id,
            "turn-cli-flow",
            ToolCall(
                "call-cli-flow",
                "fixture.echo",
                {"text": "secret tool argument must not be rendered"},
            ),
        )
        self.assertTrue(result.ok)
        self.task_id = task.task_id
        self.store = self.application.registry.require(RuntimeStorePort)
        self.expected_projection = (
            await self.application.kernel.get_flow_projection(self.task_id)
        )
        stored = await self.store.load_task(self.task_id)
        assert stored is not None
        self.version_before = stored.version
        self.events_before = await self.store.read_events(self.task_id)
        await self.application.registry.stop_all()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_human_tree_is_structured_and_does_not_render_payloads(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = await _flow(self.task_id, self.workspace, False)

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn(f"task {self.task_id}", rendered)
        self.assertIn(f"cursor={self.expected_projection.cursor}", rendered)
        self.assertIn("phase EXECUTING", rendered)
        self.assertIn("tool fixture.echo [SUCCEEDED]", rendered)
        self.assertIn("first_actionable_failure: none", rendered)
        self.assertNotIn("private goal", rendered)
        self.assertNotIn("secret tool argument", rendered)

    async def test_json_matches_projection_and_query_does_not_change_records(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = await _flow(self.task_id, self.workspace, True)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), self.expected_projection.to_data())

        verifier = SQLiteRuntimeStore(self.database)
        await verifier.start(
            AdapterContext(config={}, emit_event=lambda _type, _payload: None)
        )
        try:
            stored = await verifier.load_task(self.task_id)
            assert stored is not None
            self.assertEqual(stored.version, self.version_before)
            self.assertEqual(
                await verifier.read_events(self.task_id), self.events_before
            )
        finally:
            await verifier.stop(datetime.now(timezone.utc))

    async def test_missing_task_is_reported(self) -> None:
        with self.assertRaises(TaskNotFound):
            await _flow("missing-task", self.workspace, False)

    async def test_missing_database_is_not_created_by_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            with self.assertRaisesRegex(FileNotFoundError, "runtime database"):
                await _flow("missing-task", workspace, False)
            self.assertFalse((workspace / ".agent").exists())

    async def test_node_drill_down_is_payload_free(self) -> None:
        tool = next(
            node for node in self.expected_projection.nodes
            if node.kind.value == "tool"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result = await _flow_query(
                self.task_id, self.workspace, False, live=False,
                node_id=tool.node_id,
            )

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn(f"node {tool.node_id}", rendered)
        self.assertIn("what: tool fixture.echo", rendered)
        self.assertIn("event_anchors:", rendered)
        self.assertIn("incoming:", rendered)
        self.assertIn("outgoing:", rendered)
        self.assertNotIn("private goal", rendered)
        self.assertNotIn("secret tool argument", rendered)

    async def test_node_drill_down_json_matches_core_diagnostic(self) -> None:
        tool = next(
            node for node in self.expected_projection.nodes
            if node.kind.value == "tool"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            await _flow_query(
                self.task_id, self.workspace, True, live=False,
                node_id=tool.node_id,
            )
        data = json.loads(output.getvalue())
        expected = self.expected_projection.inspect_node(tool.node_id).to_data()
        self.assertEqual(data, {"cursor": self.expected_projection.cursor, **expected})

    async def test_node_drill_down_rejects_unknown_node(self) -> None:
        with self.assertRaisesRegex(LookupError, "flow node not found"):
            await _flow_query(
                self.task_id, self.workspace, False, live=False,
                node_id="tool:missing",
            )

    async def test_failure_shortcut_reports_absence_without_guessing(self) -> None:
        with self.assertRaisesRegex(LookupError, "no actionable failure"):
            await _flow_query(
                self.task_id, self.workspace, False, live=False, failure=True,
            )

    async def test_failure_shortcut_drills_into_first_failure(self) -> None:
        from tsm_agt.ports import RuntimeEvent, RuntimeUnitOfWork

        writer = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(self.database)
        )
        await writer.registry.start_all()
        try:
            stored = await writer.registry.require(RuntimeStorePort).load_task(
                self.task_id
            )
            assert stored is not None
            failed = TaskState.FAILED
            task_data = dict(stored.data)
            task_data["state"] = failed.value
            sequence = stored.last_event_sequence + 1
            events = (
                RuntimeEvent(
                    "failure-tool", self.task_id, sequence, "tool.failed", {
                        "turn_id": "turn-cli-flow", "execution_id": "exec-fail",
                        "result": {
                            "ok": False, "error_code": "INVALID_PARAM",
                            "message": "PRIVATE_FAILURE_MESSAGE",
                        },
                    }
                ),
                RuntimeEvent(
                    "failure-state", self.task_id, sequence + 1,
                    "task.state_changed", {"next_state": failed.value},
                ),
            )
            await writer.registry.require(RuntimeStorePort).commit(
                RuntimeUnitOfWork(
                    self.task_id, stored.version, task_data, events
                )
            )
        finally:
            await writer.registry.stop_all()
        output = io.StringIO()
        with redirect_stdout(output):
            await _flow_query(
                self.task_id, self.workspace, False, live=False, failure=True,
            )
        rendered = output.getvalue()
        self.assertIn("node tool:exec-fail", rendered)
        self.assertIn("F4 [deterministic]", rendered)
        self.assertNotIn("PRIVATE_FAILURE_MESSAGE", rendered)

    async def test_filter_tree_marks_matches_and_ancestor_context(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = await _flow_query(
                self.task_id, self.workspace, False, live=False,
                kinds=(FlowNodeKind.TOOL,),
                statuses=(FlowNodeStatus.SUCCEEDED,),
            )
        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("filters=kind=tool status=SUCCEEDED matches=1", rendered)
        self.assertIn("context task Task", rendered)
        self.assertIn("match tool fixture.echo [SUCCEEDED]", rendered)
        self.assertNotIn("phase EXECUTING", rendered)
        self.assertNotIn("secret tool argument", rendered)

    async def test_filter_json_exposes_matches_and_preserves_cursor(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            await _flow_query(
                self.task_id, self.workspace, True, live=False,
                kinds=(FlowNodeKind.TOOL,),
            )
        data = json.loads(output.getvalue())
        self.assertEqual(data["cursor"], self.expected_projection.cursor)
        self.assertEqual(data["filters"], {"kinds": ["tool"], "statuses": []})
        self.assertEqual(len(data["matched_node_ids"]), 1)
        self.assertTrue(data["matched_node_ids"][0].startswith("tool:"))
        visible_ids = {node["node_id"] for node in data["nodes"]}
        self.assertTrue(all(
            edge["from_node"] in visible_ids and edge["to_node"] in visible_ids
            for edge in data["edges"]
        ))

    async def test_node_and_filter_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            await _flow_query(
                self.task_id, self.workspace, False, live=False,
                node_id="tool:any", kinds=(FlowNodeKind.TOOL,),
            )

    async def test_export_writes_redacted_versioned_json_without_overwrite(self) -> None:
        (self.workspace / "artifacts").mkdir()
        output = io.StringIO()
        with redirect_stdout(output):
            result = await _flow_query(
                self.task_id, self.workspace, False, live=False,
                export_path=Path("artifacts/flow.json"),
            )
        self.assertEqual(result, 0)
        self.assertIn("exported: artifacts/flow.json", output.getvalue())
        exported = self.workspace / "artifacts" / "flow.json"
        document = json.loads(exported.read_text(encoding="utf-8"))
        self.assertEqual(document["artifact_type"], "tsm-agt.flow")
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["task_id"], self.task_id)
        raw = exported.read_text(encoding="utf-8")
        self.assertNotIn("private goal", raw)
        self.assertNotIn("secret tool argument", raw)
        if os.name != "nt":
            self.assertEqual(exported.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            await _flow_query(
                self.task_id, self.workspace, False, live=False,
                export_path=Path("artifacts/flow.json"),
            )

    async def test_export_can_write_filtered_view(self) -> None:
        (self.workspace / "artifacts").mkdir()
        await _flow_query(
            self.task_id, self.workspace, False, live=False,
            kinds=(FlowNodeKind.TOOL,),
            export_path=Path("artifacts/tool-flow.json"),
        )
        document = json.loads(
            (self.workspace / "artifacts" / "tool-flow.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(document["flow"]["filters"]["kinds"], ["tool"])
        self.assertEqual(len(document["flow"]["matched_node_ids"]), 1)

    async def test_timeline_and_tui_render_payload_free_swimlanes(self) -> None:
        timeline = render_flow_timeline(self.expected_projection)
        tui = render_flow_tui(self.expected_projection)
        self.assertIn("timeline task-cli-flow", timeline)
        self.assertIn("tool", timeline)
        self.assertIn("TSM-AGT FLOW", tui)
        self.assertNotIn("private goal", timeline + tui)
        self.assertNotIn("secret tool argument", timeline + tui)
        output = io.StringIO()
        with redirect_stdout(output):
            await _flow_query(
                self.task_id, self.workspace, True, live=False, timeline=True,
            )
        self.assertIn("\"lanes\"", output.getvalue())

    async def test_export_writes_redacted_jsonl_and_offline_html(self) -> None:
        (self.workspace / "artifacts").mkdir()
        await _flow_query(
            self.task_id, self.workspace, False, live=False,
            export_path=Path("artifacts/flow.jsonl"),
        )
        lines = [
            json.loads(line) for line in
            (self.workspace / "artifacts/flow.jsonl").read_text().splitlines()
        ]
        self.assertEqual(lines[0]["record_type"], "metadata")
        self.assertTrue(any(line["record_type"] == "timeline" for line in lines))
        await _flow_query(
            self.task_id, self.workspace, False, live=False,
            export_path=Path("artifacts/flow.html"),
        )
        html = (self.workspace / "artifacts/flow.html").read_text()
        self.assertIn("<!doctype html>", html)
        self.assertIn("Offline, payload-free", html)
        self.assertNotIn("private goal", html)
        self.assertNotIn("secret tool argument", html)
        self.assertNotIn("</script><script>", html)

    async def test_export_rejects_escape_suffix_missing_parent_and_bad_combinations(self) -> None:
        (self.workspace / "artifacts").mkdir()
        cases = (
            (Path("../flow.json"), "escapes"),
            (Path("artifacts/flow.txt"), ".json suffix"),
            (Path("missing/flow.json"), "parent directory"),
            (Path(".env.flow.json"), "sensitive"),
            (Path(".agent/flow.json"), "sensitive"),
        )
        for path, message in cases:
            with self.subTest(path=path):
                with self.assertRaisesRegex((PermissionError, ValueError), message):
                    await _flow_query(
                        self.task_id, self.workspace, False, live=False,
                        export_path=path,
                    )
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            await _flow_query(
                self.task_id, self.workspace, True, live=False,
                export_path=Path("artifacts/flow.json"),
            )

    async def test_live_tree_refreshes_from_cursor_and_stops_at_terminal(self) -> None:
        writer = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(self.database)
        )
        await writer.registry.start_all()
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                watcher = asyncio.create_task(
                    _flow_query(
                        self.task_id, self.workspace, False, live=True,
                        poll_interval=0.005,
                    )
                )
                await asyncio.sleep(0.02)
                for state in (
                    TaskState.VERIFYING, TaskState.FINALIZING, TaskState.SUCCEEDED
                ):
                    await writer.kernel.transition_task(
                        self.task_id, state, f"enter {state.value}"
                    )
                    await asyncio.sleep(0.02)
                self.assertEqual(
                    await asyncio.wait_for(watcher, 1.0), 0
                )
        finally:
            await writer.registry.stop_all()

        rendered = output.getvalue()
        self.assertGreaterEqual(rendered.count("--- flow update cursor="), 2)
        self.assertIn(f"task {self.task_id} [SUCCEEDED]", rendered)
        cursors = [
            int(line.rsplit("=", 1)[1].removesuffix(" ---"))
            for line in rendered.splitlines()
            if line.startswith("--- flow update cursor=")
        ]
        self.assertEqual(cursors, sorted(set(cursors)))

    async def test_replay_lists_payload_free_frames_without_mutating_store(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = await _replay(
                self.task_id, self.workspace, list_frames=True,
                at_sequence=None, as_json=False,
            )
        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn(f"replay {self.task_id}", rendered)
        self.assertIn("task.created", rendered)
        self.assertIn("tool.completed", rendered)
        self.assertNotIn("private goal", rendered)
        self.assertNotIn("secret tool argument", rendered)

        verifier = SQLiteRuntimeStore(self.database)
        await verifier.start(
            AdapterContext(config={}, emit_event=lambda _type, _payload: None)
        )
        try:
            stored = await verifier.load_task(self.task_id)
            assert stored is not None
            self.assertEqual(stored.version, self.version_before)
            self.assertEqual(await verifier.read_events(self.task_id), self.events_before)
        finally:
            await verifier.stop(datetime.now(timezone.utc))

    async def test_replay_at_sequence_json_rebuilds_historical_flow(self) -> None:
        target = next(
            event.sequence for event in self.events_before
            if event.event_type == "tool.prepared"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            await _replay(
                self.task_id, self.workspace, list_frames=False,
                at_sequence=target, as_json=True,
            )
        data = json.loads(output.getvalue())
        self.assertEqual(data["cursor"], target)
        self.assertEqual(data["latest_cursor"], self.expected_projection.cursor)
        self.assertTrue(data["read_only"])
        self.assertEqual(data["frame"]["event_type"], "tool.prepared")
        tool = next(node for node in data["flow"]["nodes"] if node["kind"] == "tool")
        self.assertEqual(tool["status"], "PENDING")
        self.assertNotIn("secret tool argument", output.getvalue())

    async def test_replay_jumps_to_node_start_and_end(self) -> None:
        tool = next(
            node for node in self.expected_projection.nodes
            if node.kind is FlowNodeKind.TOOL
        )
        start_output = io.StringIO()
        with redirect_stdout(start_output):
            await _replay(
                self.task_id, self.workspace, list_frames=False,
                at_sequence=None, node_id=tool.node_id,
                boundary=FlowReplayBoundary.START, as_json=True,
            )
        end_output = io.StringIO()
        with redirect_stdout(end_output):
            await _replay(
                self.task_id, self.workspace, list_frames=False,
                at_sequence=None, node_id=tool.node_id,
                boundary=FlowReplayBoundary.END, as_json=True,
            )
        started = json.loads(start_output.getvalue())
        finished = json.loads(end_output.getvalue())
        self.assertEqual(started["cursor"], tool.event_seq_start)
        self.assertEqual(finished["cursor"], tool.event_seq_end)
        self.assertEqual(started["target"]["boundary"], "start")
        self.assertEqual(finished["target"]["boundary"], "end")
        self.assertNotIn("secret tool argument", start_output.getvalue())

    async def test_replay_jumps_to_turn_without_prefix(self) -> None:
        writer = compose_fixture_application(
            model_adapter=ToolCallingModelProvider(),
            store_adapter=SQLiteRuntimeStore(self.database),
        )
        await writer.registry.start_all()
        try:
            task = await writer.kernel.create_task(
                "turn replay fixture", self.workspace, "task-turn-replay"
            )
            for state in (
                TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                TaskState.EXECUTING,
            ):
                task = await writer.kernel.transition_task(
                    task.task_id, state, state.value
                )
            await writer.kernel.run_agent_turn(task.task_id, "inspect turn")
            projection = await writer.kernel.get_flow_projection(task.task_id)
            turn = next(
                node for node in projection.nodes
                if node.kind is FlowNodeKind.TURN
            )
        finally:
            await writer.registry.stop_all()
        output = io.StringIO()
        with redirect_stdout(output):
            await _replay(
                task.task_id, self.workspace, list_frames=False,
                at_sequence=None, turn_id=turn.node_id.removeprefix("turn:"),
                boundary=FlowReplayBoundary.END, as_json=False,
            )
        rendered = output.getvalue()
        self.assertIn(f"target={turn.node_id}@end", rendered)
        self.assertIn(f"frame={turn.event_seq_end}/", rendered)

    async def test_replay_boundary_requires_node_or_turn(self) -> None:
        with self.assertRaisesRegex(ValueError, "only valid"):
            await _replay(
                self.task_id, self.workspace, list_frames=False,
                at_sequence=None, boundary=FlowReplayBoundary.START,
                as_json=False,
            )

    async def test_replay_playback_executes_scheduled_waits_and_json_lines(self) -> None:
        replay = FlowReplay().playback(
            self.events_before, "EXECUTING", FlowReplaySpeed.DOUBLE,
            max_wait_seconds=0.25,
            expected_latest_sequence=self.expected_projection.cursor,
        )
        waits: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waits.append(seconds)

        output = io.StringIO()
        with redirect_stdout(output):
            await _play_replay(replay, True, sleep=fake_sleep)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), len(self.events_before))
        frames = [json.loads(line) for line in lines]
        self.assertEqual(
            [frame["snapshot"]["cursor"] for frame in frames],
            list(range(1, len(self.events_before) + 1)),
        )
        self.assertTrue(all(wait <= 0.25 for wait in waits))
        self.assertNotIn("private goal", output.getvalue())
        self.assertNotIn("secret tool argument", output.getvalue())

    async def test_replay_play_max_through_readonly_sqlite(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = await _replay(
                self.task_id, self.workspace, list_frames=False,
                at_sequence=None, play=True, speed=FlowReplaySpeed.MAX,
                as_json=False,
            )
        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn("speed=max", rendered)
        self.assertEqual(
            rendered.count("--- replay frame="), len(self.events_before)
        )
        self.assertNotIn("secret tool argument", rendered)

    async def test_replay_speed_requires_play(self) -> None:
        with self.assertRaisesRegex(ValueError, "only valid with --play"):
            await _replay(
                self.task_id, self.workspace, list_frames=False,
                at_sequence=None, speed=FlowReplaySpeed.DOUBLE, as_json=False,
            )

    async def test_replay_range_step_and_cursor_resume(self) -> None:
        output = io.StringIO()
        playback = FlowReplay().playback(
            self.events_before, TaskState.EXECUTING.value,
            FlowReplaySpeed.MAX, from_sequence=2, to_sequence=4,
            expected_latest_sequence=self.expected_projection.cursor,
        )
        answers = iter(["", "q"])
        with redirect_stdout(output):
            await _play_replay(
                playback, False, input_fn=lambda _prompt: next(answers), step=True,
            )
        self.assertIn("frame=2/", output.getvalue())
        self.assertNotIn("frame=3/", output.getvalue())

        cursor_path = Path("replay-cursor.json")
        with redirect_stdout(io.StringIO()):
            await _replay(
                self.task_id, self.workspace, list_frames=False,
                at_sequence=None, play=True, speed=FlowReplaySpeed.MAX,
                from_sequence=2, to_sequence=4, save_cursor=cursor_path,
                as_json=False,
            )
        application = compose_local_flow_query_application(self.workspace)
        await application.registry.start_all()
        try:
            cursor = application.registry.require(ReplayCursorStorePort).load(
                self.workspace, str(cursor_path)
            )
        finally:
            await application.registry.stop_all()
        self.assertEqual(cursor.task_id, self.task_id)
        self.assertEqual(cursor.sequence, 4)
        output = io.StringIO()
        with redirect_stdout(output):
            await _replay(
                self.task_id, self.workspace, list_frames=False,
                at_sequence=None, play=True, speed=FlowReplaySpeed.MAX,
                to_sequence=6, resume_cursor=cursor_path, as_json=False,
            )
        self.assertIn("range=5..6", output.getvalue())

    async def test_replay_cursor_rejects_wrong_task_and_step_json(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            await _replay(
                self.task_id, self.workspace, list_frames=False,
                at_sequence=None, play=True, step=True, as_json=True,
            )
        ordinary = self.workspace / "ordinary.json"
        ordinary.write_text('{"user": "data"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported format"):
            with redirect_stdout(io.StringIO()):
                await _replay(
                    self.task_id, self.workspace, list_frames=False,
                    at_sequence=None, play=True, speed=FlowReplaySpeed.MAX,
                    from_sequence=2, to_sequence=2,
                    save_cursor=Path("ordinary.json"), as_json=False,
                )
        self.assertEqual(
            ordinary.read_text(encoding="utf-8"), '{"user": "data"}\n'
        )

    async def test_live_json_is_json_lines_without_duplicate_cursor(self) -> None:
        writer = compose_fixture_application(
            store_adapter=SQLiteRuntimeStore(self.database)
        )
        await writer.registry.start_all()
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                watcher = asyncio.create_task(
                    _flow_query(
                        self.task_id, self.workspace, True, live=True,
                        poll_interval=0.005,
                    )
                )
                await asyncio.sleep(0.02)
                for state in (
                    TaskState.VERIFYING, TaskState.FINALIZING, TaskState.SUCCEEDED
                ):
                    await writer.kernel.transition_task(
                        self.task_id, state, f"enter {state.value}"
                    )
                    await asyncio.sleep(0.02)
                await asyncio.wait_for(watcher, 1.0)
        finally:
            await writer.registry.stop_all()

        snapshots = [json.loads(line) for line in output.getvalue().splitlines()]
        cursors = [snapshot["cursor"] for snapshot in snapshots]
        self.assertGreaterEqual(len(snapshots), 2)
        self.assertEqual(cursors, sorted(set(cursors)))
        self.assertEqual(snapshots[-1]["task_id"], self.task_id)
        task_node = next(
            node for node in snapshots[-1]["nodes"] if node["kind"] == "task"
        )
        self.assertEqual(task_node["status"], "SUCCEEDED")

    def test_renderer_rejects_projection_without_task_root(self) -> None:
        broken = self.expected_projection.__class__(
            task_id=self.task_id, cursor=0, nodes=(), edges=()
        )
        with self.assertRaisesRegex(ValueError, "no task root"):
            render_flow_tree(broken)

    def test_node_renderer_reports_unknown_duration_for_running_node(self) -> None:
        task = next(
            node for node in self.expected_projection.nodes
            if node.parent_id is None
        )
        diagnostic = self.expected_projection.inspect_node(task.node_id)
        rendered = render_flow_node_diagnostic(
            diagnostic, self.expected_projection.cursor
        )
        self.assertIn("duration: in progress/unknown", rendered)
        self.assertIn("observable_facts:", rendered)
        self.assertIn("downstream_impact:", rendered)
        self.assertIn("failure_attribution:", rendered)


if __name__ == "__main__":
    unittest.main()
