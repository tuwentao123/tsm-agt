from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from tsm_agt.core import (
    FlowNodeStatus, FlowProjectionError, FlowReplay, FlowReplayBoundary,
    FlowReplaySpeed, TaskState,
)

from tests.test_flow_projector import FlowProjectorContractTest


class FlowReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events = FlowProjectorContractTest().lifecycle_events()
        self.replay = FlowReplay()

    def test_index_lists_payload_free_frames_after_full_validation(self) -> None:
        index = self.replay.index(self.events, TaskState.SUCCEEDED.value)
        self.assertEqual(index.task_id, "task-flow")
        self.assertEqual(index.latest_cursor, 14)
        self.assertTrue(index.terminal)
        self.assertEqual(index.frames[8].event_type, "tool.prepared")
        data = index.to_data()
        self.assertNotIn("payload", str(data))
        self.assertNotIn("src/app.py", str(data))

    def test_snapshot_rebuilds_exact_historical_cursor(self) -> None:
        snapshot = self.replay.snapshot(
            self.events, TaskState.SUCCEEDED.value, 6
        )
        self.assertEqual(snapshot.cursor, 6)
        self.assertEqual(snapshot.latest_cursor, 14)
        self.assertEqual(snapshot.frame.event_type, "task.state_changed")
        self.assertEqual(snapshot.projection.cursor, 6)
        root = next(
            node for node in snapshot.projection.nodes if node.parent_id is None
        )
        self.assertEqual(root.status, FlowNodeStatus.WAITING_APPROVAL)
        self.assertTrue(snapshot.to_data()["read_only"])

    def test_default_snapshot_is_latest(self) -> None:
        snapshot = self.replay.snapshot(
            self.events, TaskState.SUCCEEDED.value
        )
        self.assertEqual(snapshot.cursor, 14)
        root = next(
            node for node in snapshot.projection.nodes if node.parent_id is None
        )
        self.assertEqual(root.status, FlowNodeStatus.SUCCEEDED)

    def test_rejects_out_of_range_sequence(self) -> None:
        for sequence in (0, 15):
            with self.subTest(sequence=sequence):
                with self.assertRaisesRegex(ValueError, "between 1 and 14"):
                    self.replay.snapshot(
                        self.events, TaskState.SUCCEEDED.value, sequence
                    )

    def test_rejects_task_state_and_terminal_event_mismatch(self) -> None:
        with self.assertRaisesRegex(FlowProjectionError, "does not match"):
            self.replay.index(self.events, TaskState.FAILED.value)
        broken_final = replace(
            self.events[-1], payload={"next_state": "FAILED"}
        )
        with self.assertRaisesRegex(FlowProjectionError, "does not match"):
            self.replay.index(
                self.events[:-1] + (broken_final,), TaskState.SUCCEEDED.value
            )

    def test_rejects_event_gap_before_historical_snapshot(self) -> None:
        broken = self.events[:5] + self.events[6:]
        with self.assertRaisesRegex(FlowProjectionError, "event sequence gap"):
            self.replay.snapshot(broken, TaskState.SUCCEEDED.value, 3)

    def test_rejects_task_sequence_ahead_of_event_log_tail(self) -> None:
        with self.assertRaisesRegex(
            FlowProjectionError, "last_event_sequence does not match"
        ):
            self.replay.index(
                self.events, TaskState.SUCCEEDED.value,
                expected_latest_sequence=15,
            )

    def test_jump_to_turn_start_and_end(self) -> None:
        started = self.replay.snapshot_for_turn(
            self.events, TaskState.SUCCEEDED.value, "turn-1",
            FlowReplayBoundary.START,
        )
        finished = self.replay.snapshot_for_turn(
            self.events, TaskState.SUCCEEDED.value, "turn:turn-1",
            FlowReplayBoundary.END,
        )
        self.assertEqual(started.cursor, 3)
        self.assertEqual(started.frame.event_type, "turn.started")
        self.assertEqual(started.target.node_id, "turn:turn-1")
        self.assertEqual(started.target.boundary, FlowReplayBoundary.START)
        self.assertEqual(finished.cursor, 13)
        self.assertEqual(finished.frame.event_type, "turn.completed")

    def test_jump_to_tool_start_and_end(self) -> None:
        started = self.replay.snapshot_for_node(
            self.events, TaskState.SUCCEEDED.value, "tool:exec-1",
            FlowReplayBoundary.START,
        )
        finished = self.replay.snapshot_for_node(
            self.events, TaskState.SUCCEEDED.value, "tool:exec-1",
            FlowReplayBoundary.END,
        )
        self.assertEqual(started.cursor, 9)
        self.assertEqual(finished.cursor, 12)
        self.assertEqual(finished.target.node_kind.value, "tool")
        tool = next(
            node for node in finished.projection.nodes
            if node.node_id == "tool:exec-1"
        )
        self.assertEqual(tool.status, FlowNodeStatus.SUCCEEDED)

    def test_jump_rejects_unknown_and_unfinished_end(self) -> None:
        with self.assertRaisesRegex(LookupError, "node not found"):
            self.replay.snapshot_for_node(
                self.events, TaskState.SUCCEEDED.value, "tool:missing"
            )
        partial = self.events[:10]
        with self.assertRaisesRegex(LookupError, "no end event yet"):
            self.replay.snapshot_for_node(
                partial, TaskState.EXECUTING.value, "tool:exec-1",
                expected_latest_sequence=10,
            )

    def test_turn_jump_rejects_empty_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "turn_id must not be empty"):
            self.replay.snapshot_for_turn(
                self.events, TaskState.SUCCEEDED.value, "  "
            )

    def test_playback_builds_incremental_frames_at_1x_and_2x(self) -> None:
        realtime = self.replay.playback(
            self.events, TaskState.SUCCEEDED.value,
            FlowReplaySpeed.REALTIME, max_wait_seconds=10,
        )
        double = self.replay.playback(
            self.events, TaskState.SUCCEEDED.value,
            FlowReplaySpeed.DOUBLE, max_wait_seconds=10,
        )
        self.assertEqual(len(realtime.frames), 14)
        self.assertEqual(realtime.frames[0].wait_ms, 0)
        self.assertEqual(realtime.frames[1].recorded_gap_ms, 1000)
        self.assertEqual(realtime.frames[1].wait_ms, 1000)
        self.assertEqual(double.frames[1].wait_ms, 500)
        self.assertEqual(
            [frame.frame.sequence for frame in realtime.frames],
            list(range(1, 15)),
        )
        snapshots = list(realtime.iter_snapshots())
        self.assertEqual(snapshots[-1][1].projection.cursor, 14)
        self.assertEqual(
            [snapshot.cursor for _frame, snapshot in snapshots],
            list(range(1, 15)),
        )
        self.assertEqual(realtime.to_data()["speed"], "1x")

    def test_max_speed_has_zero_wait_and_gap_cap_is_explicit(self) -> None:
        maximum = self.replay.playback(
            self.events, TaskState.SUCCEEDED.value, FlowReplaySpeed.MAX
        )
        self.assertTrue(all(frame.wait_ms == 0 for frame in maximum.frames))
        capped = self.replay.playback(
            self.events, TaskState.SUCCEEDED.value,
            FlowReplaySpeed.REALTIME, max_wait_seconds=0.25,
        )
        self.assertEqual(capped.frames[1].recorded_gap_ms, 1000)
        self.assertEqual(capped.frames[1].wait_ms, 250)
        self.assertTrue(capped.frames[1].gap_capped)

    def test_playback_marks_regressed_timestamp_without_negative_wait(self) -> None:
        regressed = replace(
            self.events[1],
            occurred_at=self.events[0].occurred_at - timedelta(seconds=1),
        )
        playback = self.replay.playback(
            (self.events[0], regressed) + self.events[2:],
            TaskState.SUCCEEDED.value, FlowReplaySpeed.REALTIME,
        )
        self.assertEqual(playback.frames[1].recorded_gap_ms, 0)
        self.assertEqual(playback.frames[1].wait_ms, 0)
        self.assertTrue(playback.frames[1].timestamp_regressed)

    def test_playback_validates_speed_and_wait_cap(self) -> None:
        with self.assertRaisesRegex(TypeError, "FlowReplaySpeed"):
            self.replay.playback(
                self.events, TaskState.SUCCEEDED.value, "max"  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "must be positive"):
            self.replay.playback(
                self.events, TaskState.SUCCEEDED.value,
                FlowReplaySpeed.MAX, max_wait_seconds=0,
            )
        with self.assertRaisesRegex(ValueError, "must be positive"):
            self.replay.playback(
                self.events, TaskState.SUCCEEDED.value,
                FlowReplaySpeed.MAX, max_wait_seconds=float("nan"),
            )

    def test_playback_range_keeps_full_history_at_first_selected_frame(self) -> None:
        playback = self.replay.playback(
            self.events, TaskState.SUCCEEDED.value, FlowReplaySpeed.MAX,
            from_sequence=9, to_sequence=12,
        )
        snapshots = list(playback.iter_snapshots())
        self.assertEqual(playback.from_sequence, 9)
        self.assertEqual(playback.to_sequence, 12)
        self.assertEqual(
            [snapshot.cursor for _, snapshot in snapshots], [9, 10, 11, 12]
        )
        self.assertIn(
            "turn:turn-1",
            {node.node_id for node in snapshots[0][1].projection.nodes},
        )
        self.assertEqual(snapshots[-1][1].latest_cursor, 14)

    def test_playback_range_validates_boundaries_and_allows_completed_resume(self) -> None:
        with self.assertRaisesRegex(ValueError, "from sequence"):
            self.replay.playback(
                self.events, TaskState.SUCCEEDED.value,
                from_sequence=0,
            )
        with self.assertRaisesRegex(ValueError, "to sequence"):
            self.replay.playback(
                self.events, TaskState.SUCCEEDED.value,
                from_sequence=5, to_sequence=4,
            )
        completed = self.replay.playback(
            self.events, TaskState.SUCCEEDED.value,
            from_sequence=15,
        )
        self.assertEqual(completed.frames, ())


if __name__ == "__main__":
    unittest.main()
