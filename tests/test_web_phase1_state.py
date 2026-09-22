from __future__ import annotations

import importlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from tsm_agt.ports import ImageBlock
from tsm_agt.sdk.runtime import RuntimeTaskResult


web_app = importlib.import_module("tsm_agt.web.app")


class _FakeClient:
    async def get_task_result(self, _task_id: str) -> RuntimeTaskResult:
        raise AssertionError("the fake server returns the result synchronously")


class _FakeRuntimeServer:
    def __init__(self) -> None:
        self._client = _FakeClient()

    def _call(self, awaitable):
        awaitable.close()
        return RuntimeTaskResult(
            task_id="task-web",
            state="EXECUTING",
            phase1_state="RUNNING",
            status="running",
            cursor=3,
        )


class WebPhase1StateTest(unittest.IsolatedAsyncioTestCase):
    async def test_task_status_endpoint_and_ui_consume_phase1_state(self) -> None:
        previous = web_app.runtime_server
        web_app.runtime_server = _FakeRuntimeServer()
        try:
            response = await web_app.get_task("task-web")
        finally:
            web_app.runtime_server = previous

        self.assertEqual(response["phase1_state"], "RUNNING")
        self.assertIn("state.phase1_state", web_app.INDEX_HTML)
        self.assertIn("/session-input", web_app.INDEX_HTML)
        self.assertIn("const task = data.task", web_app.INDEX_HTML)
        self.assertIn("data.kind === 'answer'", web_app.INDEX_HTML)


class WorkspaceDirectoryPickerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_registry = list(web_app.workspace_registry)
        web_app.workspace_registry.clear()

    def tearDown(self) -> None:
        web_app.workspace_registry[:] = self.original_registry

    async def test_ui_uses_native_picker_instead_of_a_path_input(self) -> None:
        self.assertIn("pickWorkspace()", web_app.INDEX_HTML)
        self.assertIn("/workspaces/pick", web_app.INDEX_HTML)
        self.assertNotIn("workspace-input", web_app.INDEX_HTML)

    async def test_picked_directory_is_registered_once_and_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "workspace"
            selected.mkdir()
            unresolved = str(selected / "." / "")
            with mock.patch.object(
                web_app, "choose_workspace_directory", return_value=unresolved
            ):
                first = await web_app.pick_workspace()
                second = await web_app.pick_workspace()

        self.assertEqual(json.loads(first.body)["path"], str(selected.resolve()))
        self.assertEqual(
            json.loads(first.body)["id"], json.loads(second.body)["id"]
        )
        self.assertEqual(len(web_app.workspace_registry), 1)

    async def test_cancelled_picker_returns_no_content(self) -> None:
        with mock.patch.object(
            web_app, "choose_workspace_directory", return_value=None
        ):
            response = await web_app.pick_workspace()

        self.assertEqual(response.status_code, 204)
        self.assertEqual(web_app.workspace_registry, [])

    async def test_file_selection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "not-a-directory.txt"
            target.write_text("x", encoding="utf-8")
            with mock.patch.object(
                web_app, "choose_workspace_directory", return_value=str(target)
            ):
                with self.assertRaises(HTTPException) as caught:
                    await web_app.pick_workspace()

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(web_app.workspace_registry, [])


class PastedImageIngressTest(unittest.TestCase):
    def test_composer_accepts_pasted_images(self) -> None:
        self.assertIn("handlePaste(event)", web_app.INDEX_HTML)
        self.assertIn("clipboardData", web_app.INDEX_HTML)
        # Images must reach the session ingress, not just the local preview.
        self.assertIn("images,", web_app.INDEX_HTML)

    def test_inline_data_url_images_become_image_blocks(self) -> None:
        blocks = web_app._normalize_image_blocks([
            {"image_url": "data:image/png;base64,AAAA", "media_type": "image/png"},
            {"image_url": "data:image/webp;base64,BBBB"},
        ])

        self.assertEqual(blocks, [
            ImageBlock(image_url="data:image/png;base64,AAAA"),
            ImageBlock(image_url="data:image/webp;base64,BBBB"),
        ])
        self.assertEqual(web_app._normalize_image_blocks(None), [])

    def test_unsupported_image_source_is_reported_not_dropped(self) -> None:
        """A silently dropped attachment makes the model answer without it.

        That is what produced replies asking for a screenshot the user had
        already attached, so an unusable source has to surface as an error.
        """
        for payload in (
            [{"image_url": "https://example.com/remote.png"}],
            ["not-a-mapping"],
            [{"image_url": "data:image/png;base64,"}],
            [{"image_url": "data:image/png,AAAA"}],
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(HTTPException) as caught:
                    web_app._normalize_image_blocks(payload)
                self.assertEqual(caught.exception.status_code, 400)

    def test_base64_payloads_never_enter_the_bounded_goal_text(self) -> None:
        source = Path(web_app.__file__).read_text(encoding="utf-8")
        # A goal is bounded text; inlining base64 broke task creation with
        # "Task SPEC exceeds its bounded limits".
        self.assertNotIn("[web-ui-attached-images]\\n", source)
        self.assertNotIn("json.dumps(image_blocks)", source)
        self.assertIn("images=tuple(image_blocks)", source)

    def test_oversized_attachment_is_rejected_with_actionable_error(self) -> None:
        huge = "data:image/png;base64," + "A" * web_app.MAX_IMAGE_DATA_URL_CHARS
        with self.assertRaises(HTTPException) as caught:
            web_app._normalize_image_blocks([{"image_url": huge}])

        self.assertEqual(caught.exception.status_code, 413)
        self.assertIn("图片过大", caught.exception.detail)

    def test_ui_recompresses_images_and_explains_context_failures(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("prepareImage", html)
        self.assertIn("MAX_IMAGE_EDGE", html)
        # Every attachment is re-encoded, not only the oversized ones: a raw PNG
        # screenshot under the edge limit is the single most wasteful payload.
        self.assertIn("encodeImageToJpeg(image, MAX_IMAGE_EDGE", html)
        self.assertNotIn("if (longest <= MAX_IMAGE_EDGE) return dataUrl", html)
        # JPEG has no alpha; an opaque base keeps transparent regions readable.
        self.assertIn("fillRect(0, 0, canvas.width, canvas.height)", html)
        # The client degrades a too-large screenshot instead of hitting the 413.
        self.assertIn("IMAGE_FALLBACK_STEPS", html)
        self.assertLess(
            web_app.INDEX_HTML.count("MAX_IMAGE_DATA_URL_CHARS = 380000"), 2
        )
        # A failed task must explain itself instead of showing an empty result.
        self.assertIn("failure_reason", html)
        self.assertIn("describeFailure", html)

    def test_model_retry_status_includes_attempt_mode_and_diagnostics(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("transport_attempt", html)
        self.assertIn("max_transport_attempts", html)
        self.assertIn("transport_mode", html)
        self.assertIn("diagnostic_code", html)
        self.assertIn("diagnostic_detail", html)
        self.assertIn("recovery_action", html)

    def test_client_image_budget_stays_below_the_server_cap(self) -> None:
        """A client budget above the server cap would re-introduce the 413."""
        source = Path(web_app.__file__).read_text(encoding="utf-8")
        client_budget = int(
            re.search(r"MAX_IMAGE_DATA_URL_CHARS = (\d+);", source).group(1)
        )
        self.assertLess(client_budget, web_app.MAX_IMAGE_DATA_URL_CHARS)


class ComposerLayoutTest(unittest.TestCase):
    def test_panes_scroll_independently(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("height:100vh;overflow:hidden", html)
        self.assertIn("overscroll-behavior:contain", html)

    def test_sessions_are_nested_under_their_workspace(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("session-list", html)
        self.assertIn("workspace-group", html)
        # The separate flat session pane is gone.
        self.assertNotIn("id=\\\"conversation-list\\\"", html)

    def test_attachments_render_under_the_message_text(self) -> None:
        html = web_app.INDEX_HTML
        self.assertIn("message-images", html)
        # The send path also names its target conversation, so a reply cannot
        # land in whichever session happens to be active when it arrives.
        self.assertIn("appendMessage('user', prompt, images, conversationId)", html)


if __name__ == "__main__":
    unittest.main()
