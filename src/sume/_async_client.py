"""The asyncio client: the same methods as ``Client``, awaited."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Union
from urllib.parse import quote

import httpx

from . import _flow
from ._errors import APIConnectionError, APIError, NotFoundError, RateLimitError, SumeError
from ._files import plan_paths, read_payload
from ._http import AsyncTransport
from ._inputs import build_body, media_slots, router_for, with_media
from ._types import Event, FileInfo, JobInfo, Model, Price, Status, final_files

AsyncStatusCallback = Callable[[Status], Union[Awaitable[None], None]]


class AsyncFile(FileInfo):
    """A file a job made. ``await save()`` writes it to disk; ``await read()`` returns its bytes."""

    _transport: AsyncTransport
    _model: str | None
    _job_id: str

    def __init__(self, data: Mapping[str, Any], transport: AsyncTransport, *, model: str | None, job_id: str) -> None:
        for name, value in FileInfo.fields_from_api(data).items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_transport", transport)
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_job_id", job_id)

    async def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        target = plan_paths([self], path, model=self._model, job_id=self._job_id)[0]
        await self._transport.download(self.url, target)
        return target

    async def read(self) -> bytes:
        return await self._transport.read(self.url)

    def __repr__(self) -> str:
        return f"AsyncFile(url={self.url!r}, type={self.type!r}, content_type={self.content_type!r})"


class AsyncResult:
    """A finished job's files; see ``sume.Result``. ``save()`` is awaited."""

    def __init__(
        self,
        *,
        job_id: str,
        model: str | None,
        usd: str | None,
        result: Mapping[str, Any],
        transport: AsyncTransport,
    ) -> None:
        artifacts = [a for a in (result.get("artifacts") or []) if isinstance(a, Mapping) and a.get("url")]
        self.job_id = job_id
        self.model = model
        self.usd = usd
        self.raw = result
        self.all_files = [AsyncFile(a, transport, model=model, job_id=job_id) for a in artifacts]
        finals = {id(a) for a in final_files(artifacts)}
        self.files = [f for f, a in zip(self.all_files, artifacts) if id(a) in finals]
        self._transport = transport

    @property
    def url(self) -> str | None:
        return self.files[0].url if self.files else None

    @property
    def urls(self) -> list[str]:
        return [f.url for f in self.files]

    async def save(self, path: str | os.PathLike[str] | None = None) -> list[Path]:
        targets = plan_paths(self.files, path, model=self.model, job_id=self.job_id)
        for file, target in zip(self.files, targets):
            await self._transport.download(file.url, target)
        return targets

    def __repr__(self) -> str:
        usd = f", usd={self.usd!r}" if self.usd else ""
        return (
            f"AsyncResult(job_id={self.job_id!r}, model={self.model!r}, files={len(self.files)}{usd}, url={self.url!r})"
        )


class AsyncJob:
    """A submitted job; see ``sume.Job``. Every method is awaited."""

    def __init__(
        self,
        client: AsyncClient,
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
        self.usd = usd
        self._client = client
        self._first_delay = first_delay

    async def status(self) -> Status:
        return await self._client.status(self.id)

    async def poll(self, *, timeout: float = _flow.DEFAULT_WAIT) -> AsyncIterator[Status]:
        transport = self._client._transport
        started = time.monotonic()
        deadline = started + timeout
        delay = self._first_delay
        while True:
            if delay > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _flow.timeout_error(self.id, timeout)
                await transport.sleep(min(delay, remaining))
            try:
                response = await transport.request("GET", f"/v1/jobs/{quote(self.id)}/status", timeout=20.0)
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

    async def wait(
        self, *, timeout: float = _flow.DEFAULT_WAIT, on_status: AsyncStatusCallback | None = None
    ) -> AsyncResult:
        last: Status | None = None
        async for last in self.poll(timeout=timeout):
            if on_status is not None:
                outcome = on_status(last)
                if asyncio.iscoroutine(outcome):
                    await outcome
        assert last is not None
        transport = self._client._transport
        if not last.ok:
            info = (await transport.request("GET", f"/v1/jobs/{quote(self.id)}")).data.get("job")
            raise _flow.failure(self.id, last.state, info)
        data = (await transport.request("GET", f"/v1/jobs/{quote(self.id)}/result")).data
        model = self.model or (data.get("job") or {}).get("model")
        return AsyncResult(
            job_id=self.id, model=model, usd=self.usd, result=data.get("result") or {}, transport=transport
        )

    async def cancel(self) -> bool:
        return await self._client.cancel(self.id)

    async def events(self, *, limit: int | None = None) -> list[Event]:
        data = (
            await self._client._transport.request("GET", f"/v1/jobs/{quote(self.id)}/events", params={"limit": limit})
        ).data
        return [Event.from_api(e) for e in data.get("events") or []]

    async def info(self) -> JobInfo:
        return JobInfo.from_api(
            (await self._client._transport.request("GET", f"/v1/jobs/{quote(self.id)}")).data["job"]
        )

    def __repr__(self) -> str:
        return f"AsyncJob(id={self.id!r}, model={self.model!r})"


class AsyncClient:
    """The asyncio client. Same arguments and methods as ``sume.Client``; every call is awaited."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._transport = AsyncTransport(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries, http_client=http_client
        )
        self._models: dict[str, Model] = {}

    @property
    def base_url(self) -> str:
        return self._transport.base_url

    async def close(self) -> None:
        await self._transport.close()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def models(self, kind: str | None = None) -> list[Model]:
        data = (await self._transport.request("GET", "/v1/open/catalog", auth="optional")).data
        models = [Model.from_api(m) for m in data.get("models") or []]
        for m in models:
            self._models[m.id] = m
        return [m for m in models if kind is None or m.kind == kind]

    async def model(self, model_id: str) -> Model:
        if model_id in self._models:
            return self._models[model_id]
        try:
            data = (await self._transport.request("GET", f"/v1/open/catalog/{quote(model_id)}", auth="optional")).data
        except NotFoundError:
            known = [m.id for m in await self.models()]
            raise _flow.model_not_found(model_id, known) from None
        model = self._models[model_id] = Model.from_api(data)
        return model

    async def estimate(self, model: str, prompt: str | None = None, /, **inputs: Any) -> Price:
        info = await self.model(model)
        body = build_body(info, inputs.pop("prompt", None) if prompt is None else prompt, inputs)
        return await self._estimate(info.id, _flow.estimate_input(body))

    async def _estimate(self, model_id: str, body: Mapping[str, Any]) -> Price:
        data = (
            await self._transport.request(
                "POST", "/v1/open/estimate", json={"model": model_id, "input": dict(body)}, auth="optional", retry=True
            )
        ).data
        return Price.from_api(data)

    async def upload(self, file: Any, *, content_type: str | None = None) -> str:
        payload = read_payload(file, content_type)
        target = (
            await self._transport.request(
                "POST",
                "/v1/assets/upload-url",
                json={
                    "content_type": payload.content_type,
                    "size_bytes": len(payload.data),
                    "filename": payload.filename,
                },
            )
        ).data
        upload = target["upload"]
        await self._transport.put(
            upload["url"], payload.data, upload.get("headers") or {"content-type": payload.content_type}
        )
        asset = (
            await self._transport.request(
                "POST",
                f"/v1/assets/{quote(target['asset']['id'])}/complete",
                json={"size_bytes": len(payload.data)},
                retry=True,
            )
        ).data["asset"]
        if not asset.get("url"):
            raise SumeError(f"Sume stored {payload.filename} but returned no URL for it.", code="upload_incomplete")
        return str(asset["url"])

    async def submit(
        self,
        model: str,
        prompt: str | None = None,
        /,
        *,
        max_usd: float | Decimal | str | None = None,
        idempotency_key: str | None = None,
        **inputs: Any,
    ) -> AsyncJob:
        return await self._submit(
            model, prompt, inputs, max_usd=max_usd, idempotency_key=idempotency_key, price_it=False
        )

    async def run(
        self,
        model: str,
        prompt: str | None = None,
        /,
        *,
        max_usd: float | Decimal | str | None = None,
        timeout: float = _flow.DEFAULT_WAIT,
        idempotency_key: str | None = None,
        on_status: AsyncStatusCallback | None = None,
        **inputs: Any,
    ) -> AsyncResult:
        job = await self._submit(model, prompt, inputs, max_usd=max_usd, idempotency_key=idempotency_key, price_it=True)
        return await job.wait(timeout=timeout, on_status=on_status)

    async def _submit(
        self,
        model: str,
        prompt: str | None,
        inputs: dict[str, Any],
        *,
        max_usd: float | Decimal | str | None,
        idempotency_key: str | None,
        price_it: bool,
    ) -> AsyncJob:
        info = await self.model(model)
        body = build_body(info, inputs.pop("prompt", None) if prompt is None else prompt, inputs)
        pricing: asyncio.Task[Price] | None = None
        if price_it or max_usd is not None:
            pricing = asyncio.ensure_future(self._estimate(info.id, _flow.estimate_input(body)))
        try:
            if max_usd is not None:
                assert pricing is not None
                try:
                    price = await pricing
                except SumeError:
                    price = _flow.priced(pricing.result)
                _flow.check_budget(price, max_usd)
            urls = [await self.upload(value) for _, _, value in media_slots(body)]
            body = with_media(body, urls)
            key = idempotency_key or str(uuid.uuid4())
            try:
                data = (
                    await self._transport.request(
                        "POST",
                        f"/v1/{router_for(info)}-router/generate",
                        json={**body, "model": info.id},
                        idempotency_key=key,
                    )
                ).data
            except SumeError as error:
                if error.status is None or error.status >= 500:
                    error.idempotency_key = key
                    error.hint = (error.hint + " " if error.hint else "") + (
                        f"If the submit went through, pass idempotency_key={key!r} to get that job back."
                    )
                raise
            usd: str | None = None
            if pricing is not None:
                try:
                    usd = (await asyncio.wait_for(asyncio.shield(pricing), timeout=10)).usd
                except Exception:
                    usd = None
        finally:
            if pricing is not None and not pricing.done():
                pricing.cancel()
        job = data["job"]
        return AsyncJob(
            self,
            job["id"],
            model=job.get("model") or info.id,
            idempotency_key=key,
            replayed=bool(data.get("idempotency_hit")),
            usd=usd,
            first_delay=_flow.first_delay(data),
        )

    def job(self, job_id: str) -> AsyncJob:
        return AsyncJob(self, job_id)

    async def status(self, job_id: str) -> Status:
        return Status.from_api(
            (await self._transport.request("GET", f"/v1/jobs/{quote(job_id)}/status", timeout=20.0)).data
        )

    async def result(
        self, job_id: str, *, timeout: float = _flow.DEFAULT_WAIT, on_status: AsyncStatusCallback | None = None
    ) -> AsyncResult:
        return await AsyncJob(self, job_id).wait(timeout=timeout, on_status=on_status)

    async def cancel(self, job_id: str) -> bool:
        data = (await self._transport.request("POST", f"/v1/jobs/{quote(job_id)}/cancel", retry=True)).data
        return bool(data.get("canceled"))

    async def list_jobs(self, *, limit: int = 20, status: str | None = None) -> list[JobInfo]:
        data = (await self._transport.request("GET", "/v1/jobs", params={"limit": limit, "status": status})).data
        return [JobInfo.from_api(j) for j in data.get("jobs") or []]

    def __repr__(self) -> str:
        return f"AsyncClient(base_url={self.base_url!r})"
