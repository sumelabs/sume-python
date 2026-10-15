"""The synchronous client."""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import quote

import httpx

from . import _flow
from ._errors import APIConnectionError, APIError, NotFoundError, RateLimitError, SumeError
from ._files import plan_paths, read_payload
from ._http import SyncTransport
from ._inputs import build_body, media_slots, router_for, with_media
from ._types import Event, FileInfo, JobInfo, Model, Price, Status, final_files

StatusCallback = Callable[[Status], None]


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
        return self._transport.read(self.url)

    def __repr__(self) -> str:
        return f"File(url={self.url!r}, type={self.type!r}, content_type={self.content_type!r})"


class Result:
    """A finished job's files.

    Attributes:
        job_id: The job's id.
        model: The model that ran.
        usd: What it cost in US dollars, like ``"0.0077"``, when the price was known.
        files: The files you asked for (a video's poster frame is left out).
        all_files: Every file the job made, intermediate ones included.
        raw: The API's ``result`` object.
    """

    def __init__(
        self,
        *,
        job_id: str,
        model: str | None,
        usd: str | None,
        result: Mapping[str, Any],
        transport: SyncTransport,
    ) -> None:
        artifacts = [a for a in (result.get("artifacts") or []) if isinstance(a, Mapping) and a.get("url")]
        self.job_id = job_id
        self.model = model
        self.usd = usd
        self.raw = result
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
        self._client = client
        self._first_delay = first_delay

    def status(self) -> Status:
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

    def wait(self, *, timeout: float = _flow.DEFAULT_WAIT, on_status: StatusCallback | None = None) -> Result:
        """Wait for the job, then return its ``Result``. A failed or canceled job raises ``JobFailedError``."""
        last: Status | None = None
        for last in self.poll(timeout=timeout):
            if on_status is not None:
                on_status(last)
        assert last is not None
        if not last.ok:
            info = self._client._transport.request("GET", f"/v1/jobs/{quote(self.id)}").data.get("job")
            raise _flow.failure(self.id, last.state, info)
        data = self._client._transport.request("GET", f"/v1/jobs/{quote(self.id)}/result").data
        model = self.model or (data.get("job") or {}).get("model")
        return Result(
            job_id=self.id,
            model=model,
            usd=self.usd,
            result=data.get("result") or {},
            transport=self._client._transport,
        )

    def cancel(self) -> bool:
        """Cancel the job if generation has not started. Returns True when it was canceled (and refunded)."""
        return self._client.cancel(self.id)

    def events(self, *, limit: int | None = None) -> list[Event]:
        """The job's lifecycle: ``job.created``, ``job.started``, ``job.completed`` or ``job.failed``."""
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
        http_client: Your own ``httpx.Client``, for proxies or custom transports.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._transport = SyncTransport(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries, http_client=http_client
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

    def models(self, kind: str | None = None) -> list[Model]:
        """Every model in the open catalog, or only one ``kind``: ``image``, ``video``, ``lipsync``, ``captions``."""
        data = self._transport.request("GET", "/v1/open/catalog", auth="optional").data
        models = [Model.from_api(m) for m in data.get("models") or []]
        for m in models:
            self._models[m.id] = m
        return [m for m in models if kind is None or m.kind == kind]

    def model(self, model_id: str) -> Model:
        """One model: what it takes, its limits and its prices. Raises ``NotFoundError`` with a suggestion."""
        if model_id in self._models:
            return self._models[model_id]
        try:
            data = self._transport.request("GET", f"/v1/open/catalog/{quote(model_id)}", auth="optional").data
        except NotFoundError:
            known = [m.id for m in self.models()]
            raise _flow.model_not_found(model_id, known) from None
        model = self._models[model_id] = Model.from_api(data)
        return model

    def estimate(self, model: str, prompt: str | None = None, /, **inputs: Any) -> Price:
        """What a request would cost, in dollars. Free, and needs no key. Files are not uploaded to price it."""
        info = self.model(model)
        body = build_body(info, inputs.pop("prompt", None) if prompt is None else prompt, inputs)
        return self._estimate(info.id, _flow.estimate_input(body))

    def _estimate(self, model_id: str, body: Mapping[str, Any]) -> Price:
        data = self._transport.request(
            "POST", "/v1/open/estimate", json={"model": model_id, "input": dict(body)}, auth="optional", retry=True
        ).data
        return Price.from_api(data)

    # Files.

    def upload(self, file: Any, *, content_type: str | None = None) -> str:
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
        model: str,
        prompt: str | None = None,
        /,
        *,
        max_usd: float | Decimal | str | None = None,
        idempotency_key: str | None = None,
        **inputs: Any,
    ) -> Job:
        """Start a job and return at once. ``job.wait()`` gets its ``Result`` later.

        Inputs use the API's field names (``quality="low"``, ``duration=4``,
        ``aspect_ratio="16:9"``), plus ``image=``, ``video=``, ``audio=``,
        ``end_image=``, ``reference_images=`` and ``mask=``. Media may be URLs
        or local files; files are uploaded first.
        """
        return self._submit(model, prompt, inputs, max_usd=max_usd, idempotency_key=idempotency_key, price_it=False)

    def run(
        self,
        model: str,
        prompt: str | None = None,
        /,
        *,
        max_usd: float | Decimal | str | None = None,
        timeout: float = _flow.DEFAULT_WAIT,
        idempotency_key: str | None = None,
        on_status: StatusCallback | None = None,
        **inputs: Any,
    ) -> Result:
        """Submit, wait, and return the ``Result``: the one call most code needs.

        ``max_usd`` refuses to submit anything that costs more. ``timeout`` is how
        long to wait (the job keeps running past it). ``on_status`` is called
        with each status read.
        """
        job = self._submit(model, prompt, inputs, max_usd=max_usd, idempotency_key=idempotency_key, price_it=True)
        return job.wait(timeout=timeout, on_status=on_status)

    def _submit(
        self,
        model: str,
        prompt: str | None,
        inputs: dict[str, Any],
        *,
        max_usd: float | Decimal | str | None,
        idempotency_key: str | None,
        price_it: bool,
    ) -> Job:
        info = self.model(model)
        body = build_body(info, inputs.pop("prompt", None) if prompt is None else prompt, inputs)
        pricing: Future[Price] | None = None
        pool: ThreadPoolExecutor | None = None
        if price_it or max_usd is not None:
            # Priced beside the uploads and the submit, so knowing the cost adds no wait.
            pool = ThreadPoolExecutor(max_workers=1)
            pricing = pool.submit(self._estimate, info.id, _flow.estimate_input(body))
        try:
            if max_usd is not None:
                assert pricing is not None
                _flow.check_budget(_flow.priced(pricing.result), max_usd)
            urls = [self.upload(value) for _, _, value in media_slots(body)]
            body = with_media(body, urls)
            key = idempotency_key or str(uuid.uuid4())
            try:
                data = self._transport.request(
                    "POST",
                    f"/v1/{router_for(info)}-router/generate",
                    json={**body, "model": info.id},
                    idempotency_key=key,
                ).data
            except SumeError as error:
                # A network or server error leaves the outcome open: name the key, so a
                # retry with it adopts the job instead of paying for a second one.
                if error.status is None or error.status >= 500:
                    error.idempotency_key = key
                    error.hint = (error.hint + " " if error.hint else "") + (
                        f"If the submit went through, pass idempotency_key={key!r} to get that job back."
                    )
                raise
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
            model=job.get("model") or info.id,
            idempotency_key=key,
            replayed=bool(data.get("idempotency_hit")),
            usd=usd,
            first_delay=_flow.first_delay(data),
        )

    # Jobs.

    def job(self, job_id: str) -> Job:
        """A handle on a job you already have the id of. Makes no call."""
        return Job(self, job_id)

    def status(self, job_id: str) -> Status:
        """Read a job's state once."""
        return Status.from_api(self._transport.request("GET", f"/v1/jobs/{quote(job_id)}/status", timeout=20.0).data)

    def result(
        self, job_id: str, *, timeout: float = _flow.DEFAULT_WAIT, on_status: StatusCallback | None = None
    ) -> Result:
        """Wait for a job (if it is still running) and return its ``Result``."""
        return Job(self, job_id).wait(timeout=timeout, on_status=on_status)

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
