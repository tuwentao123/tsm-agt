"""What counts as a usable image source belongs to ImageBlock, not its callers.

``image_url`` reaches the provider verbatim, so the rule about acceptable
sources is a property of the value. It used to be a ``startswith("data:image/")``
test repeated in the Web route and in the SDK, each skipping what failed. Two
consequences followed: ``submit_task`` took already-built ImageBlocks and so was
never checked at all, and a rejected attachment vanished without a word, leaving
the model to answer a request whose screenshot it could not see.
"""

from __future__ import annotations

import inspect
import pathlib
import re
import unittest

import tsm_agt.sdk.runtime as sdk_runtime
import tsm_agt.web.app as web_app
from tsm_agt.ports import ImageBlock

PIXEL = "data:image/png;base64,iVBORw0KGgo="


class ImageBlockInvariantTest(unittest.TestCase):
    def test_inline_data_url_is_accepted(self) -> None:
        block = ImageBlock(image_url=PIXEL)
        self.assertEqual(block.detail, "auto")
        self.assertEqual(block.to_data()["type"], "input_image")

    def test_remote_link_is_refused_with_an_explanation(self) -> None:
        with self.assertRaises(ValueError) as caught:
            ImageBlock(image_url="https://cdn.example.com/shot.png")
        self.assertIn("remote links", str(caught.exception))

    def test_malformed_data_urls_are_refused(self) -> None:
        for value, expected in (
            ("", "inline base64 data URL"),
            ("data:image/png,AAAA", "inline base64 data URL"),
            ("data:text/plain;base64,AAAA", "inline base64 data URL"),
            ("data:image/;base64,AAAA", "image subtype"),
            ("data:image/png;base64,", "base64 payload"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    ImageBlock(image_url=value)
                self.assertIn(expected, str(caught.exception))

    def test_detail_level_is_constrained(self) -> None:
        for level in ("auto", "low", "high"):
            self.assertEqual(ImageBlock(PIXEL, level).detail, level)
        with self.assertRaises(ValueError):
            ImageBlock(PIXEL, "ultra")

    def test_persisted_blocks_round_trip_through_validation(self) -> None:
        """A checkpoint is replayed through the same invariant, not around it."""
        from tsm_agt.ports import Message, MessageRole, TextBlock

        message = Message(
            "msg-1", MessageRole.USER,
            (TextBlock("看下图"), ImageBlock(image_url=PIXEL)),
        )
        restored = Message.from_data(message.to_data())
        self.assertEqual(restored.content[1], ImageBlock(image_url=PIXEL))

    def test_the_source_rule_lives_in_exactly_one_place(self) -> None:
        """Re-adding a per-caller prefix test is what caused the silent drops."""
        for module in (web_app, sdk_runtime):
            source = pathlib.Path(
                inspect.getsourcefile(module) or ""
            ).read_text(encoding="utf-8")
            python_lines = [
                line for line in source.splitlines()
                # web/app.py embeds the browser bundle, whose own JPEG check is
                # about the encoder's output format, not about what may be sent.
                if 'startswith("data:image/")' in line
            ]
            self.assertEqual(
                python_lines, [],
                f"{module.__name__} re-implements the ImageBlock source rule",
            )

    def test_both_sdk_ingresses_take_validated_blocks(self) -> None:
        """Divergent signatures are why one ingress was validated and one was not."""
        for name in ("submit_task", "submit_session_text"):
            with self.subTest(method=name):
                annotation = str(
                    inspect.signature(
                        getattr(sdk_runtime.EngineeringAgentClient, name)
                    ).parameters["images"].annotation
                )
                self.assertRegex(annotation, r"tuple\[ImageBlock, \.\.\.\]")

    def test_web_translates_the_invariant_into_a_client_error(self) -> None:
        source = pathlib.Path(
            inspect.getsourcefile(web_app) or ""
        ).read_text(encoding="utf-8")
        normalize = re.search(
            r"def _normalize_image_blocks.*?\n    return blocks", source, re.S
        )
        self.assertIsNotNone(normalize)
        body = normalize.group(0)
        # No branch may skip an attachment; every rejection must raise.
        self.assertNotIn("continue", body)
        self.assertIn("status_code=400", body)


if __name__ == "__main__":
    unittest.main()
