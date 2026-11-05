from __future__ import annotations

import base64
import json
import struct

import pytest

from tsm_agt.adapters.openai_compatible import OpenAICompatibleModelProvider
from tsm_agt.adapters.openai_compatible.responses import OpenAIResponsesModelProvider
from tsm_agt.core import ContextWindowExceeded, ContextWindowManager, PromptTemplate
from tsm_agt.core.image_budget import DEFAULT_IMAGE_TOKENS, estimate_image_tokens
from tsm_agt.ports import ImageBlock, Message, MessageRole, ModelRequest, TextBlock


def image_url(header: bytes, padding: int = 0) -> str:
    return "data:image/png;base64," + base64.b64encode(header + b"\0" * padding).decode()


def png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)


def message(*blocks) -> Message:
    return Message("user", MessageRole.USER, (TextBlock("Inspect this screenshot"), *blocks))


def budget(*blocks):
    return ContextWindowManager.estimate_input_budget(
        (message(*blocks),), (), PromptTemplate.default(),
    )


def test_encoding_size_does_not_change_visual_budget():
    # Two encodings with the same pixel dimensions but very different bytes.
    small = ImageBlock(image_url(png_header(1024, 768)))
    large = ImageBlock(image_url(png_header(1024, 768), 225_000))
    small_total, small_parts = budget(small)
    large_total, large_parts = budget(large)
    assert small_total == large_total
    assert small_parts == large_parts
    images = next(part for part in large_parts if part.name == "image_inputs")
    assert images.estimated_tokens >= DEFAULT_IMAGE_TOKENS
    assert sum(part.estimated_tokens for part in large_parts) == large_total


def test_each_image_is_charged_including_repeated_images():
    image = ImageBlock(image_url(png_header(512, 512)))
    one, _ = budget(image)
    two, parts = budget(image, image)
    assert two - one >= DEFAULT_IMAGE_TOKENS
    assert next(part for part in parts if part.name == "image_inputs").estimated_tokens == 2 * estimate_image_tokens(image)


@pytest.mark.parametrize("detail", ["auto", "low", "high"])
def test_detail_never_zeroes_the_generic_image_reservation(detail):
    assert estimate_image_tokens(ImageBlock(image_url(png_header(64, 64)), detail)) >= DEFAULT_IMAGE_TOKENS


@pytest.mark.parametrize("source", [
    "data:image/png;base64,AAAA",
    "data:image/jpeg;base64,%%%%",
    "data:image/png;base64,AA",
    "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode(),
    image_url(png_header(0, 100)),
])
def test_unknown_or_damaged_headers_have_a_nonzero_fallback(source):
    assert estimate_image_tokens(ImageBlock(source)) == DEFAULT_IMAGE_TOKENS


@pytest.mark.parametrize("header", [
    png_header(4096, 2048),
    b"GIF89a" + struct.pack("<HH", 4096, 2048),
    # JPEG APP segment before the baseline frame.
    b"\xff\xd8\xff\xe0\x00\x04xx\xff\xc0" + struct.pack(">HBHHB", 8, 8, 2048, 4096, 1),
    b"\xff\xd8\xff\xc2" + struct.pack(">HBHHB", 8, 8, 2048, 4096, 1),
    b"RIFF\0\0\0\0WEBPVP8X\x0a\0\0\0" + b"\0" * 4 + (4095).to_bytes(3, "little") + (2047).to_bytes(3, "little"),
    b"RIFF\0\0\0\0WEBPVP8 \x0a\0\0\0" + b"\0" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", 4096, 2048),
    b"RIFF\0\0\0\0WEBPVP8L\x05\0\0\0\x2f" + (4095 | (2047 << 14)).to_bytes(4, "little"),
])
def test_large_dimensions_in_supported_headers_increase_the_reservation(header):
    assert estimate_image_tokens(ImageBlock(image_url(header))) == 1024 + 3 * 128 * 64


def test_text_containing_image_json_is_still_counted_as_text():
    source = image_url(png_header(1024, 768), 225_000)
    visual, _ = budget(ImageBlock(source))
    as_text, parts = budget(TextBlock(json.dumps(ImageBlock(source).to_data())))
    assert as_text > visual + 60_000
    assert not any(part.name == "image_inputs" for part in parts)


def test_large_base64_attachment_fits_without_changing_the_model_image():
    image = ImageBlock(image_url(png_header(1024, 768), 225_000), "high")
    conversation = (message(image),)
    original = conversation[0].to_data()
    prepared = ContextWindowManager().prepare(
        conversation=conversation, tools=(), prompt_template=PromptTemplate.default(),
        context_window=32_000, max_output_tokens=8192,
    )
    assert prepared.compaction is None
    assert prepared.budget.estimated_input_tokens < 32_000 - 8192
    assert prepared.canonical_messages == conversation
    assert conversation[0].to_data() == original
    report = prepared.budget.event_data()
    assert "image-reserve" in report["estimation_method"]
    assert any(part["name"] == "image_inputs" for part in report["allocations"])
    assert "base64" not in json.dumps(report)

    request = ModelRequest("test", prepared.messages)
    chat = OpenAICompatibleModelProvider("https://example.test/v1", "test", "secret")
    chat_payload, _ = chat._build_payload(request)
    assert chat_payload["messages"][-1]["content"][-1] == {
        "type": "image_url", "image_url": {"url": image.image_url, "detail": "high"},
    }
    responses = OpenAIResponsesModelProvider("https://example.test/v1", "test", "secret")
    response_payload, _ = responses._responses_payload(request)
    assert response_payload["input"][-1]["content"][-1] == {
        "type": "input_image", "image_url": image.image_url, "detail": "high",
    }


def test_large_pixel_dimensions_still_trigger_the_context_guard():
    image = ImageBlock(image_url(png_header(8192, 8192)))
    with pytest.raises(ContextWindowExceeded, match="image_inputs="):
        ContextWindowManager().prepare(
            conversation=(message(image),), tools=(), prompt_template=PromptTemplate.default(),
            context_window=128_000, max_output_tokens=8192,
        )
