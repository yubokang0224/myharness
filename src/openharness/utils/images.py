"""Small, dependency-free helpers for validating inline image payloads."""

from __future__ import annotations


def detect_supported_image_media_type(content: bytes) -> str | None:
    """Return the media type for image bytes accepted by current providers.

    File extensions and HTTP content types are only hints.  Checking the
    payload signature prevents JSON/HTML error bodies and unsupported formats
    from being forwarded as base64 image input.
    """
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"BM"):
        return "image/bmp"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None
