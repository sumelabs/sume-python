"""Turn ``run(model, prompt, **inputs)`` into the request body, and find the files to upload."""

from __future__ import annotations

import os
from typing import Any, List, Mapping, Tuple, Union

from ._errors import InvalidRequestError
from ._media import is_http_url

MEDIA_FIELDS = (
    "image_urls",
    "input_urls",
    "image_url",
    "first_frame_url",
    "end_image_url",
    "last_frame_url",
    "video_url",
    "audio_url",
    "reference_image_urls",
    "reference_video_urls",
    "reference_audio_urls",
    "mask_url",
    "mask_image_url",
)

# Short names for media fields. Each value may be a URL, a local path, bytes,
# an open file, or a PIL image; anything but a URL is uploaded first.
_VIDEO_ONLY = {
    "end_image": "end_image_url",
    "video": "video_url",
    "audio": "audio_url",
    "reference_images": "reference_image_urls",
    "reference_videos": "reference_video_urls",
    "reference_audios": "reference_audio_urls",
}
_IMAGE_ONLY = {"mask": "mask_url"}
_LIST_FIELDS = {"image_urls", "input_urls", "reference_image_urls", "reference_video_urls", "reference_audio_urls"}

SHORT_NAMES = ("image", "images", *_VIDEO_ONLY, *_IMAGE_ONLY)

Path_ = Tuple[Union[str, int], ...]
"""Where a value sits in the body: ``("image_urls", 0)``, ``("captions", "srt")``."""


def router_for(kind: str) -> str:
    """Image models go to the image router; video, lip-sync and captions to the video router."""
    return "image" if kind == "image" else "video"


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def build_body(model_id: str, kind: str, prompt: str | None, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """The router request body, without ``model``. ``None`` values are left out."""
    router = router_for(kind)
    body: dict[str, Any] = {}
    if prompt is not None:
        body["prompt"] = prompt
    for key, value in inputs.items():
        if value is None:
            continue
        if key == "model":
            raise InvalidRequestError(
                "model is the first argument, not an input.",
                hint=f'Call run("{model_id}", "<prompt>", ...).',
                code="invalid_input",
            )
        if key in ("image", "images"):
            images = _as_list(value)
            if router == "image":
                body["image_urls"] = images
            elif len(images) > 1:
                raise InvalidRequestError(
                    f"{model_id} takes one image, the first frame.",
                    hint="Pass end_image= for the last frame, or reference_images= for references.",
                    code="invalid_input",
                )
            else:
                body["image_url"] = images[0]
        elif key in _VIDEO_ONLY or key in _IMAGE_ONLY:
            wanted = "video" if key in _VIDEO_ONLY else "image"
            if router != wanted:
                article = "an" if kind[0] in "aeiou" else "a"
                raise InvalidRequestError(
                    f"{key}= applies to {wanted} models; {model_id} is {article} {kind} model.",
                    hint=f"See what {model_id} takes with sume.model({model_id!r}).",
                    code="invalid_input",
                )
            field_name = (_VIDEO_ONLY | _IMAGE_ONLY)[key]
            body[field_name] = _as_list(value) if field_name in _LIST_FIELDS else value
        else:
            body[key] = value
    # Lip-sync and captions are billed by the clip's length, which the API needs up front.
    if kind in ("lipsync", "captions") and "duration" not in body and "duration_seconds" not in body:
        what = "the clip's length in whole seconds" + (" (the audio's, for a photo)" if kind == "lipsync" else "")
        raise InvalidRequestError(
            f"{model_id} needs duration=: {what}.",
            hint=f"Pass duration=<seconds>; limits are in sume.model({model_id!r}).seconds.",
            code="invalid_input",
        )
    return body


def _is_file_object(value: Any) -> bool:
    """A value that can only mean "this file": a path object, bytes, an open file, or a PIL image."""
    if isinstance(value, (os.PathLike, bytes, bytearray, memoryview)):
        return True
    if hasattr(value, "read") and callable(value.read):
        return True
    return all(hasattr(value, attr) for attr in ("save", "mode", "size", "getpixel"))


def _needs_upload(value: Any, in_media_field: bool) -> bool:
    if isinstance(value, str):
        # A string is a file path only where a media URL belongs; elsewhere it is text.
        return in_media_field and not is_http_url(value) and not value.startswith("data:")
    return _is_file_object(value)


def upload_slots(body: Mapping[str, Any]) -> list[tuple[Path_, Any]]:
    """Every value to upload before submitting, with where it sits, in a stable order.

    File objects (paths, bytes, open files, PIL images) are uploaded wherever
    they are, nested included. Plain strings are uploaded only where a media URL
    belongs: a media field, or the ``url`` of a nested ``image_url``,
    ``video_url`` or ``audio_url``.
    """
    slots: list[tuple[Path_, Any]] = []

    def walk(value: Any, path: Path_, in_media_field: bool) -> None:
        if isinstance(value, Mapping):
            parent = path[-1]
            for key, item in value.items():
                # {"image_url": {"url": ...}} inside a list like frame_images is a media URL too.
                media = in_media_field or (key == "url" and isinstance(parent, str) and parent.endswith("_url"))
                walk(item, (*path, key), media)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, (*path, index), in_media_field)
        elif _needs_upload(value, in_media_field):
            slots.append((path, value))

    for key, value in body.items():
        walk(value, (key,), key in MEDIA_FIELDS)
    return slots


def with_uploads(body: Mapping[str, Any], urls: List[str]) -> dict[str, Any]:
    """A copy of ``body`` with each upload slot (in ``upload_slots`` order) replaced by ``urls``."""

    def copy(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {k: copy(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [copy(v) for v in value]
        return value

    out: dict[str, Any] = copy(body)
    for (path, _), url in zip(upload_slots(body), urls):
        target: Any = out
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = url
    return out


def describe(value: Any) -> str:
    """A short name for a media value, for messages: a path, a file name, or its kind."""
    if isinstance(value, (str, os.PathLike)):
        return os.path.basename(os.fspath(value))
    name = getattr(value, "name", None) or getattr(value, "filename", None)
    if isinstance(name, str) and name:
        return os.path.basename(name)
    return type(value).__name__
