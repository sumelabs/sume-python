"""The synchronous client."""

from __future__ import annotations

import logging
import os
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Union
from urllib.parse import quote

import httpx
from typing_extensions import Unpack

from . import _flow
from ._errors import APIConnectionError, APIError, InvalidRequestError, NotFoundError, RateLimitError, SumeError
from ._files import FileInput, plan_paths, read_payload
from ._generated import MODEL_KINDS, Inputs, ModelId
from ._http import SyncTransport
from ._inputs import build_body, router_for, take_prompt, upload_slots, with_uploads
from ._types import Event, FileInfo, JobInfo, Model, ModelKind, Price, Status, files_html, final_files

logger = logging.getLogger("sume")

StatusCallback = Callable[[Status], None]
EventCallback = Callable[[Event], None]
SubmitCallback = Callable[["Job"], None]
Budget = Union[float, Decimal, str]


class File(FileInfo):
    """A file a job made. ``save()`` writes it to disk; ``read()`` returns its bytes."""

    _transport: SyncTransport
    _model: str | None
    _job_id: str

    def __init__(self, data: Mapping[str, Any], transport: SyncTransport, *, model: str | None, job_id: str) -> None:
        for name, value in FileInfo.fields_from_api(data).items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_transport", transport)
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_job_id", job_id)

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Download to ``path`` (a file or a directory; default: ``<model>-<job>.<ext>`` here). Returns the path."""
        target = plan_paths([self], path, model=self._model, job_id=self._job_id)[0]
        self._transport.download(self.url, target)
        return target

    def read(self) -> bytes:
        """The file's bytes, without saving it."""
        return self._transport.read(self.url)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "type": self.type,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "duration_ms": self.duration_ms,
        }

    def __repr__(self) -> str:
        return f"File(url={self.url!r}, type={self.type!r}, content_type={self.content_type!r})"


class Result:
    """A finished job's files.

    Attributes:
        job_id: The job's id.
        model: The model that ran.
        usd: What it cost in US dollars, like ``"0.0060"``, when the price was known.
        files: The files you asked for (a video's poster frame is left out).
        all_files: Every file the job made, intermediate ones included.
        job: The job as the API describes it: input, timing (``job.seconds``), and so on.
        raw: The API's ``result`` object.
    """

    def __init__(
        self,
        *,
        job_id: str,
        model: str | None,
        usd: str | None,
        result: Mapping[str, Any],
        job: Mapping[str, Any] | None,
        transport: SyncTransport,
    ) -> None:
        artifacts = [a for a in (result.get("artifacts") or []) if isinstance(a, Mapping) and a.get("url")]
        self.job_id = job_id
        self.model = model
        self.usd = usd
        self.raw = result
        self.job: JobInfo | None = JobInfo.from_api(job) if isinstance(job, Mapping) and job.get("id") else None
        self.all_files = [File(a, transport, model=model, job_id=job_id) for a in artifacts]
        finals = {id(a) for a in final_files(artifacts)}
        self.files = [f for f, a in zip(self.all_files, artifacts) if id(a) in finals]
        self._transport = transport

    @property
    def url(self) -> str | None:
        """The first file's URL."""
        return self.files[0].url if self.files else None

    @property
    def urls(self) -> list[str]:
        return [f.url for f in self.files]

    def save(self, path: str | os.PathLike[str] | None = None) -> list[Path]:
        """Download every file. ``path`` may be a file name (the rest are numbered), a directory, or nothing.

        Returns the paths written, in order.
        """
        targets = plan_paths(self.files, path, model=self.model, job_id=self.job_id)
        for file, target in zip(self.files, targets):
            self._transport.download(file.url, target)
        return targets

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-ready summary, for returning from a web handler."""
        return {
            "job_id": self.job_id,
            "model": self.model,
            "usd": self.usd,
            "url": self.url,
            "files": [f.to_dict() for f in self.files],
            "seconds": self.job.seconds if self.job else None,
        }

    def _repr_html_(self) -> str:
        """In a notebook, a result shows its image or plays its video."""
        usd = f" · ${self.usd}" if self.usd else ""
        return files_html(self.files, f"{self.model} · {self.job_id}{usd}")

    def __repr__(self) -> str:
        usd = f", usd={self.usd!r}" if self.usd else ""
        return f"Result(job_id={self.job_id!r}, model={self.model!r}, files={len(self.files)}{usd}, url={self.url!r})"


class Job:
    """A submitted job. ``wait()`` returns its ``Result``; ``status()`` reads its state once."""

    def __init__(
        self,
        client: Client,
        job_id: str,
        *,
        model: str | None = None,
        idempotency_key: str | None = None,
        replayed: bool = False,
        usd: str | None = None,
        first_delay: float = 0.0,
    ) -> None:
        self.id = job_id
        self.model = model
        self.idempotency_key = idempotency_key
        self.replayed = replayed
        """True when the idempotency key matched an earlier submit: this one paid nothing."""
        self.usd = usd
        """The price in dollars, when it was known at submit."""
        self._client = client
        self._first_delay = first_delay

    def status(self) -> Status:
        """Read the job's state once: ``queued``, ``processing``, ``completed``, ``failed`` or ``canceled``."""
        return self._client.status(self.id)

    def poll(self, *, timeout: float = _flow.DEFAULT_WAIT) -> Iterator[Status]:
        """Yield each status read until the job completes, fails or is canceled.

        Waits between reads as the API asks, up to 5 to 15 s. Network errors
        and Sume-side errors do not end the wait; ``timeout`` does
        (``WaitTimeoutError``, and the job keeps running).
        """
        transport = self._client._transport
        started = time.monotonic()
        deadline = started + timeout
        delay = self._first_delay
        while True:
            if delay > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _flow.timeout_error(self.id, timeout)
                transport.sleep(min(delay, remaining))
            try:
                response = transport.request("GET", f"/v1/jobs/{quote(self.id)}/status", timeout=20.0)
            except (APIConnectionError, APIError, RateLimitError):
                delay = 5.0
                continue
            status = Status.from_api(response.data)
            if self.model is None:
                self.model = (response.data.get("job") or {}).get("model")
            yield status
            if status.done:
                return
            if time.monotonic() >= deadline:
                raise _flow.timeout_error(self.id, timeout)
            delay = _flow.next_delay(status, response.headers, time.monotonic() - started)

    def wait(
        self,
        *,
        timeout: float = _flow.DEFAULT_WAIT,
        on_status: StatusCallback | None = None,
        on_event: EventCallback | None = None,
        cancel_on_timeout: bool = False,
    ) -> Result:
        """Wait for the job, then return its ``Result``. A failed or canceled job raises ``JobFailedError``.

        Args:
            timeout: Seconds to wait. Past it, ``WaitTimeoutError``; the job keeps running.
            on_status: Called with each status read (``status.state``, ``status.queue_position``).
            on_event: Called once with each new event in the job's life (``job.started``, …).
            cancel_on_timeout: When the wait runs out, cancel the job if it has not started (it is refunded).
        """
        seen: set[str] = set()
        last: Status | None = None
        try:
            for last in self.poll(timeout=timeout):
                if on_status is not None:
                    on_status(last)
                if on_event is not None:
                    self._emit_new_events(seen, on_event)
        except SumeError as error:
            if cancel_on_timeout and error.code == "wait_timeout":
                try:
                    canceled = self.cancel()
                except SumeError:
                    canceled = False
                if canceled:
                    raise _flow.timeout_error(self.id, timeout, canceled=True) from None
            raise
        assert last is not None
        transport = self._client._transport
        if not last.ok:
            info = transport.request("GET", f"/v1/jobs/{quote(self.id)}").data.get("job")
            raise _flow.failure(self.id, last.state, info)
        data = transport.request("GET", f"/v1/jobs/{quote(self.id)}/result").data
        model = self.model or (data.get("job") or {}).get("model")
        return Result(
            job_id=self.id,
            model=model,
            usd=self.usd,
            result=data.get("result") or {},
            job=data.get("job"),
            transport=transport,
        )

    def _emit_new_events(self, seen: set[str], on_event: EventCallback) -> None:
        try:
            events = self.events()
        except SumeError:
            return
        for event in events:
            key = event.id or f"{event.type}@{event.created_at}"
            if key not in seen:
                seen.add(key)
                on_event(event)

    def cancel(self) -> bool:
        """Cancel the job if generation has not started. Returns True when it was canceled (and refunded)."""
        return self._client.cancel(self.id)

    def events(self, *, limit: int | None = None) -> list[Event]:
        """The job's life so far: ``job.created``, ``job.started``, ``job.completed`` or ``job.failed``."""
        data = self._client._transport.request("GET", f"/v1/jobs/{quote(self.id)}/events", params={"limit": limit}).data
        return [Event.from_api(e) for e in data.get("events") or []]

    def info(self) -> JobInfo:
        """Everything the API knows about the job: input, state, timing, error, files."""
        return JobInfo.from_api(self._client._transport.request("GET", f"/v1/jobs/{quote(self.id)}").data["job"])

    def __repr__(self) -> str:
        return f"Job(id={self.id!r}, model={self.model!r})"


class Client:
    """A client for the Sume Open API.

    Args:
        api_key: The key. Default: ``SUME_API_KEY``, then ``SUME_API_KEY`` in ``./.env``.
        base_url: The API. Default: ``SUME_API_BASE_URL``, then ``https://api.open.sume.com``.
        timeout: Seconds per HTTP request (waiting for a job has its own ``timeout``).
        max_retries: Retries for requests that are safe to repeat: reads, and submits,
            which carry an idempotency key so a retry never pays twice.
        headers: Extra headers for every API call (never sent to storage or media hosts).
        http_client: Your own ``httpx.Client``, for proxies or custom transports.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        headers: Mapping[str, str] | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._transport = SyncTransport(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
            headers=headers,
        )
        self._models: dict[str, Model] = {}

    @property
    def base_url(self) -> str:
        return self._transport.base_url

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Models and prices: public, no key needed.

    def models(self, kind: ModelKind | None = None) -> list[Model]:
        """Every model in the open catalog, or only one ``kind``: ``image``, ``video``, ``lipsync``, ``captions``.

        Example:
            >>> for m in sume.models("video"):
            ...     print(m.id, m.price, m.seconds)
            seedance-2-mini $0.02936–$0.06352/s (4, 15)
        """
        data = self._transport.request("GET", "/v1/open/catalog", auth="optional").data
        models = [Model.from_api(m) for m in data.get("models") or []]
        for m in models:
            self._models[m.id] = m
        return [m for m in models if kind is None or m.kind == kind]

    def model(self, model_id: Union[ModelId, str]) -> Model:
        """One model: what it takes, its limits and its prices. Raises ``NotFoundError`` with a suggestion."""
        if model_id in self._models:
            return self._models[model_id]
        try:
            data = self._transport.request("GET", f"/v1/open/catalog/{quote(model_id)}", auth="optional").data
        except NotFoundError:
            raise self._not_found(model_id) from None
        model = self._models[model_id] = Model.from_api(data)
        return model

    def _not_found(self, model_id: str) -> NotFoundError:
        try:
            known = [m.id for m in self.models()]
        except SumeError:
            known = list(MODEL_KINDS)
        return _flow.model_not_found(model_id, known)

    def _field_hint(self, model_id: str, error: InvalidRequestError) -> str:
        try:
            capabilities = self.model(model_id).raw.get("capabilities")
        except SumeError:
            capabilities = None
        return _flow.field_hint(model_id, capabilities, error.field_errors)

    def _kind(self, model_id: str) -> str:
        """The model's kind, from the ids this version knows, else from the catalog."""
        known = MODEL_KINDS.get(model_id)
        return known if known is not None else self.model(model_id).kind

    def estimate(
        self,
        model: Union[ModelId, str],
        prompt: str | None = None,
        /,
        *,
        input: Optional[Mapping[str, Any]] = None,
        **inputs: Unpack[Inputs],
    ) -> Price:
        """What a request would cost, in dollars. Free, and needs no key. Files are not uploaded to price it.

        Example:
            >>> str(sume.estimate("seedance-2-mini", "a paper boat", duration=8, resolution="720p"))
            '$0.5082'
        """
        fields: Dict[str, Any] = {**(input or {}), **inputs}
        body = build_body(model, self._kind(model), take_prompt(prompt, fields), fields)
        return self._estimate(model, _flow.estimate_input(body))

    def _estimate(self, model_id: str, body: Mapping[str, Any]) -> Price:
        data = self._transport.request(
            "POST", "/v1/open/estimate", json={"model": model_id, "input": dict(body)}, auth="optional", retry=True
        ).data
        return Price.from_api(data)

    # Files.

    def upload(self, file: FileInput, *, content_type: str | None = None) -> str:
        """Put a file in Sume storage and return its URL, for any media input.

        ``file`` may be a path, bytes, an open binary file, or a PIL image.
        You rarely need this: ``run`` and ``submit`` upload local files themselves.
        """
        payload = read_payload(file, content_type)
        target = self._transport.request(
            "POST",
            "/v1/assets/upload-url",
            json={"content_type": payload.content_type, "size_bytes": len(payload.data), "filename": payload.filename},
        ).data
        upload = target["upload"]
        self._transport.put(
            upload["url"], payload.data, upload.get("headers") or {"content-type": payload.content_type}
        )
        asset = self._transport.request(
            "POST",
            f"/v1/assets/{quote(target['asset']['id'])}/complete",
            json={"size_bytes": len(payload.data)},
            retry=True,
        ).data["asset"]
        if not asset.get("url"):
            raise SumeError(f"Sume stored {payload.filename} but returned no URL for it.", code="upload_incomplete")
        return str(asset["url"])

    # Generation.

    def submit(
        self,
        model: Union[ModelId, str],
        prompt: str | None = None,
        /,
        *,
        max_usd: Budget | None = None,
        idempotency_key: str | None = None,
        input: Optional[Mapping[str, Any]] = None,
        **inputs: Unpack[Inputs],
    ) -> Job:
        """Start a job and return at once. ``job.wait()`` gets its ``Result`` later.

        Takes the same inputs as ``run``. When the account already has as many jobs
        running as it may, ``submit`` waits for a free slot (up to 10 minutes) rather
        than failing.

        Example:
            >>> job = sume.submit("kling-3", "a red kite over a beach", duration=5)
            >>> video = job.wait()
        """
        fields: Dict[str, Any] = {**(input or {}), **inputs}
        return self._submit(
            model,
            prompt,
            fields,
            max_usd=max_usd,
            idempotency_key=idempotency_key,
            price_it=False,
            slot_wait=_flow.SLOT_WAIT,
        )

    def run(
        self,
        model: Union[ModelId, str],
        prompt: str | None = None,
        /,
        *,
        max_usd: Budget | None = None,
        timeout: float = _flow.DEFAULT_WAIT,
        idempotency_key: str | None = None,
        on_submit: SubmitCallback | None = None,
        on_status: StatusCallback | None = None,
        on_event: EventCallback | None = None,
        cancel_on_timeout: bool = False,
        input: Optional[Mapping[str, Any]] = None,
        **inputs: Unpack[Inputs],
    ) -> Result:
        """Submit, wait, and return the ``Result``: the one call most code needs.

        Args:
            model: A model id, like ``"gpt-image-2"`` or ``"seedance-2-mini"``. ``sume.models()`` lists them.
            prompt: What to make.
            max_usd: Refuse to submit anything that costs more, before uploading or charging.
            timeout: Seconds to wait in all (for a free slot and for the job). The job keeps running past it.
            idempotency_key: Reuse an earlier submit's key to get that job instead of paying again.
            on_submit: Called with the ``Job`` once it exists, so you can keep its id.
            on_status: Called with each status read.
            on_event: Called with each new event in the job's life.
            cancel_on_timeout: Cancel the job (and get refunded) if the wait runs out before it starts.
            input: Request fields as a dict, for any field the keyword arguments do not list.
            **inputs: Request fields by the API's names (``quality``, ``duration``, ``resolution``, …) and
                media by short name (``image``, ``video``, ``audio``, …). Files are uploaded first.

        Example:
            >>> image = sume.run("gpt-image-2", "a red kite over a beach", quality="low")
            >>> image.save("kite.png")
            [PosixPath('/home/you/kite.png')]
        """
        started = time.monotonic()
        fields: Dict[str, Any] = {**(input or {}), **inputs}
        job = self._submit(
            model,
            prompt,
            fields,
            max_usd=max_usd,
            idempotency_key=idempotency_key,
            price_it=True,
            slot_wait=timeout,
        )
        if on_submit is not None:
            on_submit(job)
        remaining = max(timeout - (time.monotonic() - started), 0.001)
        return job.wait(timeout=remaining, on_status=on_status, on_event=on_event, cancel_on_timeout=cancel_on_timeout)

    def _submit(
        self,
        model: str,
        prompt: str | None,
        fields: Dict[str, Any],
        *,
        max_usd: Budget | None,
        idempotency_key: str | None,
        price_it: bool,
        slot_wait: float,
    ) -> Job:
        kind = self._kind(model)
        body = build_body(model, kind, take_prompt(prompt, fields), fields)
        pricing: Future[Price] | None = None
        pool: ThreadPoolExecutor | None = None
        if price_it or max_usd is not None:
            # Priced beside the uploads and the submit, so knowing the cost adds no wait.
            pool = ThreadPoolExecutor(max_workers=1)
            pricing = pool.submit(self._estimate, model, _flow.estimate_input(body))
        try:
            if max_usd is not None:
                assert pricing is not None
                _flow.check_budget(_flow.priced(pricing.result), max_usd)
            urls = [self.upload(value) for _, value in upload_slots(body)]
            body = with_uploads(body, urls)
            key = idempotency_key or str(uuid.uuid4())
            data = self._generate(model, kind, body, key, slot_wait)
            usd: str | None = None
            if pricing is not None:
                try:
                    usd = pricing.result(timeout=10).usd
                except Exception:
                    usd = None
        finally:
            if pool is not None:
                pool.shutdown(wait=False)
        job = data["job"]
        return Job(
            self,
            job["id"],
            model=job.get("model") or model,
            idempotency_key=key,
            replayed=bool(data.get("idempotency_hit")),
            usd=usd,
            first_delay=_flow.first_delay(data),
        )

    def _generate(self, model: str, kind: str, body: Mapping[str, Any], key: str, slot_wait: float) -> Any:
        deadline = time.monotonic() + slot_wait
        attempt = 0
        while True:
            try:
                return self._transport.request(
                    "POST",
                    f"/v1/{router_for(kind)}-router/generate",
                    json={**body, "model": model},
                    idempotency_key=key,
                ).data
            except NotFoundError as error:
                if error.code == "model_not_found":
                    raise self._not_found(model) from None
                raise
            except InvalidRequestError as error:
                if error.field_errors:
                    error.hint = self._field_hint(model, error)
                raise
            except SumeError as error:
                if _flow.is_full(error):
                    # The account has as many jobs running as it may: wait for a slot, then ask again.
                    attempt += 1
                    delay = _flow.slot_delay(error, attempt)
                    if time.monotonic() + delay < deadline:
                        logger.info("All job slots are busy; asking again in %.0f s", delay)
                        self._transport.sleep(delay)
                        continue
                # A network or server error leaves the outcome open: name the key, so a
                # retry with it adopts the job instead of paying for a second one.
                if error.status is None or error.status >= 500:
                    error.idempotency_key = key
                    error.hint = (error.hint + " " if error.hint else "") + (
                        f"If the submit went through, pass idempotency_key={key!r} to get that job back."
                    )
                raise

    # Jobs.

    def job(self, job_id: str) -> Job:
        """A handle on a job you already have the id of. Makes no call."""
        return Job(self, job_id)

    def status(self, job_id: str) -> Status:
        """Read a job's state once."""
        return Status.from_api(self._transport.request("GET", f"/v1/jobs/{quote(job_id)}/status", timeout=20.0).data)

    def result(
        self,
        job_id: str,
        *,
        timeout: float = _flow.DEFAULT_WAIT,
        on_status: StatusCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> Result:
        """Wait for a job (if it is still running) and return its ``Result``."""
        return Job(self, job_id).wait(timeout=timeout, on_status=on_status, on_event=on_event)

    def cancel(self, job_id: str) -> bool:
        """Cancel a job that has not started generating. Returns True when it was canceled; it is refunded."""
        data = self._transport.request("POST", f"/v1/jobs/{quote(job_id)}/cancel", retry=True).data
        return bool(data.get("canceled"))

    def list_jobs(self, *, limit: int = 20, status: str | None = None) -> list[JobInfo]:
        """Your newest jobs first. ``status`` keeps one state: ``queued``, ``processing``, ``completed``, …"""
        data = self._transport.request("GET", "/v1/jobs", params={"limit": limit, "status": status}).data
        return [JobInfo.from_api(j) for j in data.get("jobs") or []]

    def __repr__(self) -> str:
        return f"Client(base_url={self.base_url!r})"
