"""The plain objects the client returns. Each keeps the API's own JSON in ``raw``."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence

from typing_extensions import Literal

JobState = Literal["queued", "processing", "completed", "failed", "canceled"]
"""Where a job is: ``queued``, ``processing``, ``completed``, ``failed`` or ``canceled``."""

ModelKind = Literal["image", "video", "lipsync", "captions"]

TERMINAL_STATES = ("completed", "failed", "canceled")

_UNITS = {"second": "/s", "image": "/image", "request": "/request"}


def _inputs(modes: Sequence[str]) -> tuple[str, ...]:
    """``text-to-video, image-to-video`` -> ``(text, image)``: what a model starts from."""
    words: list[str] = []
    for mode in modes:
        word = "image" if mode == "image-edit" else "video" if mode == "video-captions" else mode.split("-to-")[0]
        if word not in words:
            words.append(word)
    return tuple(words)


@dataclass(frozen=True)
class Model:
    """A model in the open catalog: what it makes, what it takes, and its price per image or second."""

    id: str
    name: str
    kind: ModelKind
    """``image``, ``video``, ``lipsync`` or ``captions``."""
    company: str
    price_from: str
    """Dollars per ``per`` for the cheapest option, like ``"0.0077"``."""
    price_to: str | None
    """Dollars per ``per`` for the dearest option, when it differs."""
    per: str
    """``image`` or ``second``."""
    inputs: tuple[str, ...]
    modes: tuple[str, ...]
    resolutions: tuple[str, ...]
    seconds: tuple[int, int] | None
    """The shortest and longest clip, for video, lip-sync and captions."""
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Model:
        pricing = data.get("pricing") or {}
        caps = data.get("capabilities") or {}
        span = caps.get("duration_seconds") if isinstance(caps, Mapping) else None
        seconds = (
            (int(span["min"]), int(span["max"]))
            if isinstance(span, Mapping)
            and isinstance(span.get("min"), (int, float))
            and isinstance(span.get("max"), (int, float))
            else None
        )
        to = pricing.get("to_usd")
        modes = tuple(data.get("modes") or ())
        return cls(
            id=data["id"],
            name=data.get("name") or data["id"],
            kind=data.get("kind") or "image",
            company=(data.get("company") or {}).get("name", ""),
            price_from=str(pricing.get("from_usd", "")),
            price_to=str(to) if to and to != pricing.get("from_usd") else None,
            per=pricing.get("unit") or "request",
            inputs=_inputs(modes),
            modes=modes,
            resolutions=tuple(caps.get("resolutions") or ()) if isinstance(caps, Mapping) else (),
            seconds=seconds,
            raw=data,
        )

    @property
    def price(self) -> str:
        """``$0.0077/image`` or ``$0.0879–$0.1890/s``."""
        unit = _UNITS.get(self.per, f"/{self.per}")
        return f"${self.price_from}{f'–${self.price_to}' if self.price_to else ''}{unit}"

    def __str__(self) -> str:
        return f"{self.id} ({self.kind}, {self.price})"


@dataclass(frozen=True)
class Price:
    """What a request costs, in US dollars. Pricing is free and needs no key."""

    model: str
    usd: str
    """Dollars, as the API writes them: ``"0.0077"``."""
    usd_micros: int
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Price:
        return cls(
            model=data.get("model", ""), usd=str(data["usd"]), usd_micros=int(data.get("usd_micros", 0)), raw=data
        )

    @property
    def dollars(self) -> Decimal:
        return Decimal(self.usd)

    def __str__(self) -> str:
        return f"${self.usd}"


@dataclass(frozen=True)
class Status:
    """One read of a job's state."""

    job_id: str
    state: JobState
    queue_position: int | None
    next_poll_after_seconds: float | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Status:
        state = data.get("sume_status") or str(data.get("status", "")).lower()
        if state in ("in_queue", "in_progress"):
            state = "queued" if state == "in_queue" else "processing"
        return cls(
            job_id=data.get("job_id") or (data.get("job") or {}).get("id", ""),
            state=state,  # type: ignore[arg-type]
            queue_position=data.get("queue_position"),
            next_poll_after_seconds=data.get("next_poll_after_seconds"),
            raw=data,
        )

    @property
    def done(self) -> bool:
        """True once the job has completed, failed or been canceled."""
        return bool(self.raw.get("terminal")) or self.state in TERMINAL_STATES

    @property
    def ok(self) -> bool:
        return self.state == "completed"

    def __str__(self) -> str:
        if self.state == "queued" and self.queue_position is not None:
            return f"queued (#{self.queue_position} in line)"
        return self.state


@dataclass(frozen=True)
class Event:
    """A step in a job's life, like ``job.started`` or ``job.failed``."""

    id: str
    type: str
    message: str | None
    created_at: str
    data: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def raw(self) -> Mapping[str, Any]:
        """The API's JSON for the event, like every other type's ``raw``."""
        return self.data

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Event:
        return cls(
            id=data.get("id", ""),
            type=data.get("type", ""),
            message=data.get("message"),
            created_at=data.get("created_at", ""),
            data=data,
        )


@dataclass(frozen=True)
class FileInfo:
    """A file a job made, by URL. ``Result.files`` holds the downloadable versions."""

    url: str
    type: str
    """``image``, ``video``, ``audio`` or ``file``."""
    content_type: str | None
    size_bytes: int | None
    width: int | None
    height: int | None
    duration_ms: int | None
    stage: str | None
    """Set on intermediate files, like a video's ``poster`` frame."""

    @classmethod
    def fields_from_api(cls, data: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "url": data["url"],
            "type": data.get("type", "file"),
            "content_type": data.get("content_type"),
            "size_bytes": data.get("size_bytes"),
            "width": data.get("width"),
            "height": data.get("height"),
            "duration_ms": data.get("duration_ms"),
            "stage": data.get("stage"),
        }


def final_files(artifacts: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The files a person asked for: no intermediate stages, and no poster image beside a video."""
    finals = [a for a in artifacts if not a.get("stage")]
    if any(a.get("type") == "video" for a in finals):
        finals = [a for a in finals if a.get("type") != "image"]
    return finals or list(artifacts)


@dataclass(frozen=True)
class JobInfo:
    """A job as ``GET /v1/jobs/{id}`` describes it: what it ran with, where it is, and how long it took."""

    id: str
    model: str | None
    state: JobState
    created_at: str
    started_at: str | None
    completed_at: str | None
    error: Mapping[str, Any] | None
    input: Mapping[str, Any]
    idempotency_key: str | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> JobInfo:
        return cls(
            id=data["id"],
            model=data.get("model"),
            state=data.get("status", ""),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            input=data.get("input") or {},
            idempotency_key=data.get("idempotency_key"),
            raw=data,
        )

    @property
    def seconds(self) -> float | None:
        """How long the job ran, from start (or creation) to completion, when it has completed."""
        from datetime import datetime

        def parse(value: str | None) -> datetime | None:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
            except ValueError:
                return None

        start = parse(self.started_at) or parse(self.created_at)
        end = parse(self.completed_at)
        return round((end - start).total_seconds(), 3) if start and end else None

    @property
    def urls(self) -> list[str]:
        result = self.raw.get("result") or {}
        return [a["url"] for a in final_files(result.get("artifacts") or []) if a.get("url")]


@dataclass(frozen=True)
class FieldError:
    """One field the API refused, like ``duration: must be between 4 and 15``."""

    path: str
    """The field, dotted for nested ones (``captions.style``); empty when the whole request is at fault."""
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message
