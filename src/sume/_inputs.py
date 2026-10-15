"""Turn ``run(model, prompt, **inputs)`` into the request body, and find the files to upload."""

from __future__ import annotations

import os
from typing import Any, Iterator, Mapping

from ._errors import InvalidRequestError
from ._media import is_http_url
from ._types import Model

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


def router_for(model: Model) -> str:
    """Image models go to the image router; video, lip-sync and captions to the video router."""
    return "image" if model.kind == "image" else "video"


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def build_body(model: Model, prompt: str | None, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """The router request body, without ``model``. ``None`` values are left out."""
    router = router_for(model)
    body: dict[str, Any] = {}
    if prompt is not None:
        body["prompt"] = prompt
    for key, value in inputs.items():
        if value is None:
            continue
        if key == "model":
            raise InvalidRequestError(
                "model is the first argument, not an input.",
                hint=f'Call run("{model.id}", "<prompt>", ...).',
                code="invalid_input",
            )
        if key in ("image", "images"):
            images = _as_list(value)
            if router == "image":
                body["image_urls"] = images
            elif len(images) > 1:
                raise InvalidRequestError(
                    f"{model.id} takes one image, the first frame.",
                    hint="Pass end_image= for the last frame, or reference_images= for references.",
                    code="invalid_input",
                )
            else:
                body["image_url"] = images[0]
        elif key in _VIDEO_ONLY or key in _IMAGE_ONLY:
            wanted = "video" if key in _VIDEO_ONLY else "image"
            if router != wanted:
                article = "an" if model.kind[0] in "aeiou" else "a"
                raise InvalidRequestError(
                    f"{key}= applies to {wanted} models; {model.id} is {article} {model.kind} model.",
                    hint=f"See what {model.id} takes with sume.model({model.id!r}).",
                    code="invalid_input",
                )
            field_name = (_VIDEO_ONLY | _IMAGE_ONLY)[key]
            body[field_name] = _as_list(value) if field_name in _LIST_FIELDS else value
        else:
            body[key] = value
    # Lip-sync and captions are billed by the clip's length, which the API needs up front.
    if model.kind in ("lipsync", "captions") and "duration" not in body and "duration_seconds" not in body:
        what = "the clip's length in whole seconds" + (" (the audio's, for a photo)" if model.kind == "lipsync" else "")
        raise InvalidRequestError(
            f"{model.id} needs duration=: {what}.",
            hint=f"Pass duration=<seconds>; limits are in sume.model({model.id!r}).seconds.",
            code="invalid_input",
        )
    return body


def needs_upload(value: Any) -> bool:
    """Anything in a media field that is not already a URL is a file to upload."""
    if isinstance(value, str):
        return not is_http_url(value) and not value.startswith("data:")
    return value is not None


def media_slots(body: Mapping[str, Any]) -> Iterator[tuple[str, int | None, Any]]:
    """``(field, index, value)`` for every media value that must be uploaded first."""
    for name in MEDIA_FIELDS:
        value = body.get(name)
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if needs_upload(item):
                    yield name, index, item
        elif value is not None and needs_upload(value):
            yield name, None, value


def with_media(body: Mapping[str, Any], urls: list[str]) -> dict[str, Any]:
    """A copy of ``body`` with each media slot (in ``media_slots`` order) replaced by ``urls``."""
    out: dict[str, Any] = {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in body.items()}
    for (name, index, _), url in zip(list(media_slots(body)), urls):
        if index is None:
            out[name] = url
        else:
            out[name][index] = url
    return out


def describe(value: Any) -> str:
    """A short name for a media value, for messages: a path, a file name, or its kind."""
    if isinstance(value, (str, os.PathLike)):
        return os.path.basename(os.fspath(value))
    name = getattr(value, "name", None) or getattr(value, "filename", None)
    if isinstance(name, str) and name:
        return os.path.basename(name)
    return type(value).__name__
