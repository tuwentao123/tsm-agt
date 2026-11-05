"""Bounded image-header inspection for provider-neutral context estimates.

This is a conservative fallback, not a provider tokenizer or billing formula.
Transport encoding size must not price a structured image as ordinary text.
Only headers are decoded; no pixels are decompressed and no I/O is performed.
"""

from __future__ import annotations

import base64
import binascii

from tsm_agt.ports import ImageBlock


# Web attachments are normally at most 1024px on their longest edge. Reserve
# 4096 tokens for those images, including low/auto detail: detail semantics vary
# by route. Larger SDK/CLI images scale up without a cap rather than becoming
# artificially cheap. Unknown/unsupported headers retain the same reservation.
DEFAULT_IMAGE_TOKENS = 4096
_HEADER_BASE64_CHARS = 87_384  # At most ~64 KiB, independent of attachment size.


def estimate_image_tokens(image: ImageBlock) -> int:
    dimensions = _image_dimensions(image.image_url)
    if dimensions is None:
        return DEFAULT_IMAGE_TOKENS
    width, height = dimensions
    patches = ((width + 31) // 32) * ((height + 31) // 32)
    return max(DEFAULT_IMAGE_TOKENS, 1024 + 3 * patches)


def _image_dimensions(data_url: str) -> tuple[int, int] | None:
    start = data_url.find(";base64,")
    if start < 0:
        return None
    encoded = data_url[start + 8:start + 8 + _HEADER_BASE64_CHARS]
    try:
        header = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None

    width = height = 0
    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24 and header[12:16] == b"IHDR":
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
    elif header[:6] in (b"GIF87a", b"GIF89a") and len(header) >= 10:
        width = int.from_bytes(header[6:8], "little")
        height = int.from_bytes(header[8:10], "little")
    elif header.startswith(b"\xff\xd8"):
        # JPEG APP/metadata segments can precede the start-of-frame dimensions.
        offset = 2
        while offset + 4 <= len(header):
            if header[offset] != 0xFF:
                break
            offset += 1
            while offset < len(header) and header[offset] == 0xFF:
                offset += 1
            if offset >= len(header):
                break
            marker = header[offset]
            offset += 1
            if marker in (0xD9, 0xDA):  # end of image / compressed scan data
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue
            length = int.from_bytes(header[offset:offset + 2], "big")
            if length < 2 or offset + length > len(header):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and length >= 8:
                height = int.from_bytes(header[offset + 3:offset + 5], "big")
                width = int.from_bytes(header[offset + 5:offset + 7], "big")
                break
            offset += length
    elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        if header[12:16] == b"VP8X" and len(header) >= 30:
            width = 1 + int.from_bytes(header[24:27], "little")
            height = 1 + int.from_bytes(header[27:30], "little")
        elif header[12:16] == b"VP8 " and len(header) >= 30 and header[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(header[26:28], "little") & 0x3FFF
            height = int.from_bytes(header[28:30], "little") & 0x3FFF
        elif header[12:16] == b"VP8L" and len(header) >= 25 and header[20] == 0x2F:
            bits = int.from_bytes(header[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
    return (width, height) if width > 0 and height > 0 else None
