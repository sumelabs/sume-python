"""Reading files to upload and choosing where downloads go."""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Sequence, Union

from typing_extensions import Protocol

from ._errors import InvalidRequestError
from ._media import EXTENSIONS_LIST, content_type_for_name, extension_for, sniff_content_type


class SupportsImageSave(Protocol):
    """A PIL image, or anything else with PIL's ``save(fp, format=...)``."""

    def save(self, fp: Any, format: Any = ..., **params: Any) -> None: ...


FileInput = Union[str, "os.PathLike[str]", bytes, bytearray, memoryview, IO[bytes], SupportsImageSave]
"""Anything the client can upload: a URL (sent as is), a file path, bytes, an open binary file, or a PIL image."""


@dataclass(frozen=True)
class Payload:
    """Bytes ready to upload."""

    data: bytes
    content_type: str
    filename: str


def read_payload(value: Any, content_type: str | None = None) -> Payload:
    """A path, bytes, an open binary file, or a PIL image, as bytes with a media type."""
    if isinstance(value, (str, os.PathLike)):
        path = Path(os.fspath(value)).expanduser()
        if not path.is_file():
            raise InvalidRequestError(
                f"No such file: {os.fspath(value)}",
                hint="Pass a public https URL, or the path of a file that exists.",
                code="file_not_found",
            )
        data = path.read_bytes()
        name = path.name
        kind = content_type or content_type_for_name(name) or sniff_content_type(data)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        name = "upload"
        kind = content_type or sniff_content_type(data)
    elif _is_pil_image(value):
        buffer = io.BytesIO()
        value.save(buffer, format="PNG")
        data = buffer.getvalue()
        name = "image.png"
        kind = "image/png"
    elif hasattr(value, "read"):
        data = value.read()
        if isinstance(data, str):
            raise InvalidRequestError(
                "The file was opened in text mode.",
                hint='Open it with open(path, "rb").',
                code="invalid_input",
            )
        raw_name = getattr(value, "name", None)
        name = os.path.basename(raw_name) if isinstance(raw_name, str) else "upload"
        kind = content_type or content_type_for_name(name) or sniff_content_type(data)
    else:
        raise InvalidRequestError(
            f"Cannot upload a {type(value).__name__}.",
            hint="Pass a URL, a file path, bytes, an open binary file, or a PIL image.",
            code="invalid_input",
        )
    if not data:
        raise InvalidRequestError(f"{name} is empty.", code="empty_file")
    if not kind:
        raise InvalidRequestError(
            f"Cannot tell the media type of {name}.",
            hint=f"Pass content_type=, or use a file ending in one of: {EXTENSIONS_LIST}.",
            code="unknown_media_type",
        )
    if "." not in name:
        name += extension_for(kind)
    return Payload(data=data, content_type=kind, filename=name)


def _is_pil_image(value: Any) -> bool:
    return all(hasattr(value, attr) for attr in ("save", "mode", "size", "getpixel"))


def short_job_id(job_id: str) -> str:
    """``job_7513b52d-3669-…`` -> ``7513b52d``."""
    return re.sub(r"^job_", "", job_id).split("-")[0][:8] or job_id


def plan_paths(
    files: Sequence[Any],
    path: str | os.PathLike[str] | None,
    *,
    model: str | None,
    job_id: str,
) -> list[Path]:
    """Where each file goes.

    No path: the working directory, as ``<model>-<job>.<ext>``, never
    overwriting a file. A directory (existing, or ending in ``/``): the same
    names inside it. A file name: the first file takes it, the rest are
    numbered (``kite.png``, ``kite-2.png``).
    """
    exts = [extension_for(f.content_type, f.url, f.type) for f in files]
    stem = f"{re.sub(r'[^A-Za-z0-9._-]+', '-', model or 'sume')}-{short_job_id(job_id)}"
    many = len(files) > 1
    text = os.fspath(path) if path is not None else None
    target = Path(text).expanduser().absolute() if text else None
    if target is None or text.endswith(("/", os.sep)) or target.is_dir():  # type: ignore[union-attr]
        directory = target or Path.cwd().absolute()
        taken: set[Path] = set()
        out: list[Path] = []
        for index, ext in enumerate(exts):
            base = f"{stem}-{index + 1}" if many else stem
            candidate = directory / f"{base}{ext}"
            n = 2
            while candidate in taken or candidate.exists():
                candidate = directory / f"{base}-{n}{ext}"
                n += 1
            taken.add(candidate)
            out.append(candidate)
        return out
    suffix = target.suffix
    base_path = target.with_suffix("") if suffix else target
    return [
        Path(f"{base_path}{suffix or ext}") if index == 0 else Path(f"{base_path}-{index + 1}{ext}")
        for index, ext in enumerate(exts)
    ]
