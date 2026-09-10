"""Opt-in smoke journey for a real OpenAI-compatible Provider.

The test creates a synthetic temporary project and never reads repository source.
Run explicitly with TSM_AGT_RUN_REAL_PROVIDER_JOURNEY=1; normal CI stays offline.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tsm_agt.bootstrap import (
    compose_openai_compatible_readonly_application_from_env,
)
from tsm_agt.core import AcceptanceStatus, TaskOutcomeStatus, TaskState
from tsm_agt.ports import RuntimeStorePort


@unittest.skipUnless(
    os.environ.get("TSM_AGT_RUN_REAL_PROVIDER_JOURNEY") == "1",
    "real Provider journey is opt-in",
)
class RealProviderJourneyTest(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_project_analysis_reaches_verified_answer(self):
        env_file = Path(os.environ["TSM_AGT_REAL_PROVIDER_ENV_FILE"]).resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = "SYNTHETIC_ARCH_MARKER_7391"
            (root / "pyproject.toml").write_text(
                "[project]\nname='synthetic-agent-smoke'\n"
                "[project.scripts]\nsynthetic-cli='synthetic_app:main'\n",
                encoding="utf-8",
            )
            (root / "synthetic_app.py").write_text(
                f'MARKER = "{marker}"\n\n'
                "def main():\n    return MARKER\n",
                encoding="utf-8",
            )
            application = compose_openai_compatible_readonly_application_from_env(
                env_file=env_file,
                database_path=root / ".agent" / "runtime.db",
            )
            await application.registry.start_all()
            try:
                session = await application.kernel.create_session(
                    "real provider synthetic journey"
                )
                goal = (
                    "Inspect this synthetic project. State its CLI entry point and "
                    f"the exact marker {marker}. Use workspace evidence first."
                )
                task = await application.kernel.create_task(
                    goal, root, session_id=session.session_id
                )
                for state in (
                    TaskState.INTAKE, TaskState.RESOLVING_PROJECT,
                    TaskState.SELECTING_EXTENSIONS, TaskState.ROUTING,
                    TaskState.EXECUTING,
                ):
                    task = await application.kernel.transition_task(
                        task.task_id, state, f"real-provider {state.value.lower()}"
                    )
                result = await application.kernel.run_agent_turn(
                    task.task_id, goal, max_model_calls=8, max_tool_calls=12
                )
                self.assertIn("synthetic-cli", result.assistant_message.text)
                self.assertIn(marker, result.assistant_message.text)
                self.assertGreaterEqual(result.tool_calls, 1)

                await application.kernel.transition_task(
                    task.task_id, TaskState.VERIFYING,
                    "verify real provider synthetic answer",
                )
                verification = await application.kernel.verify_task_acceptance(
                    task.task_id
                )
                verification_detail = {
                    "criteria": [
                        {
                            "criterion_id": item.criterion_id,
                            "status": item.status.value,
                            "evidence": [evidence.to_data() for evidence in item.evidence],
                        }
                        for item in verification.criteria
                    ],
                    "outcomes": [
                        outcome.to_data() for outcome in (
                            await application.kernel.get_task_spec(task.task_id)
                        ).outcomes
                    ],
                }
                self.assertEqual(
                    verification.status, AcceptanceStatus.PASSED,
                    verification_detail,
                )
                outcomes = (await application.kernel.get_task_spec(task.task_id)).outcomes
                self.assertTrue(outcomes)
                self.assertTrue(all(
                    outcome.status is TaskOutcomeStatus.DELIVERED
                    for outcome in outcomes if outcome.required
                ))
                events = await application.registry.require(
                    RuntimeStorePort
                ).read_events(task.task_id)
                self.assertTrue(any(
                    event.event_type == "tool.completed" for event in events
                ))
                self.assertFalse(any(
                    event.event_type in {"llm.failed", "turn.failed"}
                    for event in events
                ))
            finally:
                await application.registry.stop_all()


if __name__ == "__main__":
    unittest.main()
