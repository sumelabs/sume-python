"""Media types: from a file name, from the bytes themselves, and back to an extension."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}

_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "model/gltf-binary": ".glb",
    "application/json": ".json",
}

EXTENSIONS_LIST = " ".join(sorted(_TYPES))


def content_type_for_name(name: str) -> str | None:
    """``photo.PNG`` -> ``image/png``; None for an extension we do not know."""
    return _TYPES.get(os.path.splitext(name)[1].lower())


def sniff_content_type(data: bytes) -> str | None:
    """The media type from the first bytes, for uploads that have no file name."""
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"mif1"):
            return "image/heic"
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        return "video/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    return None


def extension_for(content_type: str | None, url: str | None = None, kind: str | None = None) -> str:
    """The file extension for a result: its content type first, then its URL, then its kind."""
    if content_type:
        ext = _EXTENSIONS.get(content_type.split(";")[0].strip().lower())
        if ext:
            return ext
    if url:
        ext = os.path.splitext(urlsplit(url).path)[1].lower()
        if ext in _TYPES:
            return ext
    return {"image": ".png", "video": ".mp4", "audio": ".mp3"}.get(kind or "", ".bin")


def is_http_url(value: str) -> bool:
    return value[:8].lower().startswith(("http://", "https://"))
