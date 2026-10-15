#!/usr/bin/env python3
"""Generate src/sume/_generated.py from the API: the model ids and the request fields.

    python scripts/generate.py            # rewrite the generated file
    python scripts/generate.py --check    # exit 1 if the API has moved on

The model ids come from the open catalog (GET /v1/open/catalog); the fields,
their types and their descriptions from the two router request schemas in
GET /v1/openapi.json. SUME_API_BASE_URL picks the API (default
https://api.open.sume.com). Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

BASE = (os.environ.get("SUME_API_BASE_URL") or "https://api.open.sume.com").rstrip("/").removesuffix("/v1")
OUT = Path(__file__).resolve().parent.parent / "src" / "sume" / "_generated.py"
ROUTERS = {"image": "/v1/image-router/generate", "video": "/v1/video-router/generate"}
# Fields the client sets itself, or that the API keeps only for old callers.
SKIP = {"model", "mode", "wait_timeout_seconds"}
MEDIA_FIELDS = {
    "image_urls",
    "input_urls",
    "image_url",
    "end_image_url",
    "video_url",
    "audio_url",
    "reference_image_urls",
    "reference_video_urls",
    "reference_audio_urls",
    "mask_url",
    "mask_image_url",
}
# The client's short names for media fields, documented here so they autocomplete.
SHORT_NAMES: dict[str, tuple[str, str]] = {
    "image": (
        "Union[FileInput, List[FileInput]]",
        "The input image: a URL, a file path, bytes, an open file or a PIL image. Image models take "
        "several (image_urls); video models take one, the first frame (image_url). Uploaded first "
        "when it is not a URL.",
    ),
    "images": ("List[FileInput]", "Several input images, for image models (image_urls). Uploaded first when not URLs."),
    "end_image": ("FileInput", "The last frame, for video models (end_image_url). Uploaded first when not a URL."),
    "video": ("FileInput", "The input video (video_url). Uploaded first when not a URL."),
    "audio": ("FileInput", "The input audio (audio_url). Uploaded first when not a URL."),
    "reference_images": ("List[FileInput]", "Reference images (reference_image_urls). Uploaded first when not URLs."),
    "reference_videos": ("List[FileInput]", "Reference videos (reference_video_urls). Uploaded first when not URLs."),
    "reference_audios": ("List[FileInput]", "Reference audio (reference_audio_urls). Uploaded first when not URLs."),
    "mask": ("FileInput", "The edit mask, for image models (mask_url). Uploaded first when not a URL."),
}


def get(path: str) -> Any:
    request = urllib.request.Request(f"{BASE}{path}", headers={"user-agent": "sume-python generate"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def resolve(node: Any, spec: dict[str, Any], seen: frozenset[str] = frozenset()) -> Any:
    if isinstance(node, list):
        return [resolve(n, spec, seen) for n in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref in seen:
            return {}
        target: Any = spec
        for part in ref.removeprefix("#/").split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return resolve(target, spec, seen | {ref})
    if "allOf" in node:
        merged: dict[str, Any] = {k: v for k, v in node.items() if k != "allOf"}
        for part in resolve(node["allOf"], spec, seen):
            merged.setdefault("properties", {}).update(part.get("properties", {}))
            merged["required"] = [*merged.get("required", []), *part.get("required", [])]
            for key, value in part.items():
                if key not in ("properties", "required"):
                    merged.setdefault(key, value)
        return resolve(merged, spec, seen)
    return {k: resolve(v, spec, seen) for k, v in node.items()}


def doc(text: str | None, limit: int = 400) -> str:
    """The description, cut at a sentence end so hovers stay readable."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) > limit:
        cut = text.rfind(". ", 0, limit)
        text = text[: cut + 1] if cut > 80 else text[:limit].rstrip() + "…"
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def literal(values: list[Any]) -> str:
    return "Literal[" + ", ".join(json.dumps(v) for v in values) + "]"


class Writer:
    def __init__(self) -> None:
        self.nested: list[str] = []

    def type_of(self, name: str, schema: dict[str, Any], *, media: bool = False) -> str:
        if media:
            return "List[FileInput]" if schema.get("type") == "array" else "FileInput"
        options = schema.get("oneOf") or schema.get("anyOf")
        if options:
            parts = []
            for option in options:
                part = self.type_of(name, option)
                if part not in parts:
                    parts.append(part)
            # A literal next to plain str adds nothing a checker can use.
            if "str" in parts:
                parts = [p for p in parts if not p.startswith("Literal[")]
            return parts[0] if len(parts) == 1 else "Union[" + ", ".join(parts) + "]"
        kind = schema.get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), "string")
        if schema.get("enum") and kind in ("string", "integer", None):
            return literal(schema["enum"])
        if kind == "string":
            return "str"
        if kind == "integer":
            return "int"
        if kind == "number":
            return "float"
        if kind == "boolean":
            return "bool"
        if kind == "array":
            return f"List[{self.type_of(name, schema.get('items') or {})}]"
        if kind == "object" and schema.get("properties"):
            return self.typed_dict(name, schema)
        return "Dict[str, Any]"

    def typed_dict(self, name: str, schema: dict[str, Any]) -> str:
        class_name = "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part)
        lines = [f"class {class_name}(TypedDict, total=False):"]
        if schema.get("description"):
            lines.append(f'    """{doc(schema["description"], 300)}"""\n')
        for key, field in schema["properties"].items():
            if field.get("deprecated"):
                continue
            # A nested `url` inside an `image_url`/`video_url`/`audio_url` object is a media input too.
            media = key == "url" and name.endswith("_url")
            lines.append(f"    {key}: {self.type_of(f'{name}_{key}', field, media=media)}")
            if field.get("description"):
                lines.append(f'    """{doc(field["description"])}"""')
        text = "\n".join(lines) + "\n"
        if text not in self.nested:
            self.nested.append(text)
        return class_name


def main() -> int:
    spec = get("/v1/openapi.json")
    catalog = get("/v1/open/catalog")["data"]["models"]
    ids = sorted(m["id"] for m in catalog)
    kinds = {m["id"]: m["kind"] for m in sorted(catalog, key=lambda m: m["id"])}
    image_ids = [i for i in ids if kinds[i] == "image"]
    video_ids = [i for i in ids if kinds[i] != "image"]

    writer = Writer()
    per_router: dict[str, dict[str, tuple[str, str]]] = {}
    for router, path in ROUTERS.items():
        schema = resolve(spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"], spec)
        for key, field in schema["properties"].items():
            if key in SKIP or field.get("deprecated"):
                continue
            type_ = writer.type_of(key, field, media=key in MEDIA_FIELDS)
            per_router.setdefault(key, {})[router] = (type_, doc(field.get("description")))

    fields: dict[str, tuple[str, str]] = {}
    for key, routers in per_router.items():
        types = [t for t, _ in routers.values()]
        if len(set(types)) == 1:
            type_ = types[0]
        elif all(t.startswith("Literal[") for t in types):
            values = sorted({v for t in types for v in re.findall(r'"[^"]*"', t)})
            type_ = f"Literal[{', '.join(values)}]"
        else:
            type_ = "Union[" + ", ".join(dict.fromkeys(types)) + "]"
        docs = {router: text for router, (_, text) in routers.items() if text}
        if len(routers) == 1:
            (router,) = routers
            description = f"{router.capitalize()} models only. {docs.get(router, '')}".strip()
        elif len(set(docs.values())) <= 1:
            description = next(iter(docs.values()), "")
        else:
            description = " ".join(f"{router.capitalize()} models: {text}" for router, text in docs.items())
        fields[key] = (type_, description)

    out = [
        f"# Generated by scripts/generate.py from {BASE}: the open catalog ({len(ids)} models) and the",
        f"# router request schemas in {spec.get('info', {}).get('title', 'the OpenAPI document')} "
        f"{spec.get('info', {}).get('version', '')}. Do not edit by hand: run `python scripts/generate.py`.",
        '"""Model ids and request fields, for type checkers and editors. Other ids and fields still work."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any, Dict, List, Union",
        "",
        "from typing_extensions import Literal, TypedDict",
        "",
        "from ._files import FileInput",
        "",
        f"ModelId = {literal(ids)}",
        '"""Every model in the open catalog when this version was built. `sume.models()` lists the live catalog."""',
        "",
        f"ImageModelId = {literal(image_ids)}",
        '"""Models that make images."""',
        "",
        f"VideoModelId = {literal(video_ids)}",
        '"""Models that make video: text and image to video, lip-sync, and captions."""',
        "",
        "MODEL_KINDS: Dict[str, str] = " + json.dumps(kinds, indent=4).replace("\n}", ",\n}"),
        '"""Each known model\'s kind, so a run needs no catalog lookup to pick its router."""',
        "",
        "",
    ]
    out.extend(f"{text}\n" for text in writer.nested)
    out.append("class Inputs(TypedDict, total=False):")
    out.append(
        '    """Request fields, by the API\'s own names, plus short names for media. A model uses the ones it takes;\n'
        '    `sume.model(id)` says which. Fields that are not listed here still work, through `input=`."""\n'
    )
    for key, (type_, text) in SHORT_NAMES.items():
        out.append(f"    {key}: {type_}")
        out.append(f'    """{doc(text)}"""')
    for key, (type_, description) in fields.items():
        out.append(f"    {key}: {type_}")
        if description:
            out.append(f'    """{description}"""')
    source = "\n".join(out).rstrip() + "\n"

    if "--check" in sys.argv:
        if OUT.read_text(encoding="utf-8") != source:
            print("src/sume/_generated.py is behind the API. Run `python scripts/generate.py`.", file=sys.stderr)
            return 1
        print("Generated types match the API.")
        return 0
    OUT.write_text(source, encoding="utf-8")
    print(f"Wrote {len(ids)} model ids and {len(fields) + len(SHORT_NAMES)} fields to {OUT.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
