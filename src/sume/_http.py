"""HTTP for the API: the key, the ``data``/``error`` envelopes, request ids, and retrying what is safe."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import random
import sys
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx

from ._config import resolve_api_key, resolve_base_url
from ._constants import KEYS_URL
from ._errors import APIConnectionError, APIError, AuthenticationError, SumeError, error_from_response
from ._version import __version__

logger = logging.getLogger("sume")
"""Debug lines for each API call: method, path, status, request id, time. Never the key or an upload URL."""

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_RETRY_DELAY = 30.0
TRANSFER_TIMEOUT = httpx.Timeout(30.0, read=300.0, write=300.0)
USER_AGENT = f"sume-python/{__version__} (python {platform.python_version()}; {sys.platform})"


@dataclass(frozen=True)
class Response:
    data: Any
    status: int
    request_id: str | None
    headers: Mapping[str, str]


def _sleep_sync(seconds: float) -> None:
    time.sleep(seconds)


async def _sleep_async(seconds: float) -> None:
    await asyncio.sleep(seconds)


def backoff(attempt: int) -> float:
    """0.5 s, 1 s, 2 s, … with ±25% jitter, capped at 8 s."""
    return min(0.5 * 2.0 ** (attempt - 1), 8.0) * (0.75 + random.random() * 0.5)


def retry_delay(headers: Mapping[str, str], error: SumeError, attempt: int) -> float:
    """The API's Retry-After (header, then body) when it gave one, else backoff; never over 30 s."""
    try:
        seconds = float(headers.get("retry-after") or 0)
    except ValueError:
        seconds = 0.0
    seconds = seconds or float(error.retry_after or 0)
    return min(seconds, MAX_RETRY_DELAY) if seconds > 0 else backoff(attempt)


class _Base:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float,
        max_retries: int,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if base_url is not None:
            resolve_base_url(base_url)  # refuse a bad URL at construction, not at the first call
        self._explicit_base = base_url
        self._explicit_key = api_key
        self._extra_headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.timeout = timeout
        self.max_retries = max_retries

    @property
    def base_url(self) -> str:
        # Resolved per call, like the key: SUME_API_BASE_URL set after import still counts.
        return resolve_base_url(self._explicit_base)

    def _key(self, auth: str) -> str | None:
        # Resolved per call, so a key exported after import is still found.
        key = resolve_api_key(self._explicit_key)
        if key is None and auth == "required":
            raise AuthenticationError(
                "No API key.",
                hint=f"Set SUME_API_KEY (or put it in ./.env, or pass api_key=). A person makes the key at {KEYS_URL}.",
                code="missing_api_key",
            )
        return key

    def _headers(self, key: str | None, idempotency_key: str | None, has_body: bool) -> dict[str, str]:
        headers = {"accept": "application/json", "user-agent": USER_AGENT, **self._extra_headers}
        if key:
            headers["authorization"] = f"Bearer {key}"
        if idempotency_key:
            headers["idempotency-key"] = idempotency_key
        if has_body:
            headers["content-type"] = "application/json"
        return headers

    def _parse(self, method: str, path: str, response: httpx.Response) -> tuple[Response | None, SumeError | None]:
        request_id = response.headers.get("x-sume-request-id")
        try:
            payload = response.json() if response.content else None
        except ValueError:
            payload = None
        if response.is_success:
            if not isinstance(payload, dict) or "data" not in payload:
                return None, APIError(
                    f"Unexpected response from {method} {path} ({response.status_code}).",
                    code="unexpected_response",
                    status=response.status_code,
                    request_id=request_id,
                )
            return Response(payload["data"], response.status_code, request_id, response.headers), None
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            snippet = response.text.strip()[:200]
            error = {
                "code": f"http_{response.status_code}",
                "message": f"The API answered {response.status_code}: {snippet}"
                if snippet
                else f"The API answered {response.status_code}.",
            }
        return None, error_from_response(response.status_code, error, request_id=request_id, base_url=self.base_url)

    def _connection_error(self, url: str, error: Exception) -> APIConnectionError:
        host = httpx.URL(url).host
        reason = "timed out" if isinstance(error, httpx.TimeoutException) else type(error).__name__
        return APIConnectionError(
            f"Could not reach {host} ({reason}).",
            hint=f"Check your connection, and base_url (now {self.base_url}).",
            code="network_error",
            retryable=True,
        )


def _log(method: str, path: str, response: httpx.Response, attempt: int, started: float) -> None:
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "%s %s -> %s (request %s, attempt %d, %d ms)",
            method,
            path,
            response.status_code,
            response.headers.get("x-sume-request-id", "-"),
            attempt,
            round((time.monotonic() - started) * 1000),
        )


def _retryable(error: SumeError) -> bool:
    return error.status in RETRY_STATUSES and error.code not in ("concurrency_limit_reached", "queue_full")


class SyncTransport(_Base):
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float,
        max_retries: int,
        http_client: httpx.Client | None,
        sleep: Callable[[float], None] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries, headers=headers)
        self.http = http_client or httpx.Client(follow_redirects=True)
        self._owns_http = http_client is None
        # Looked up at call time, so tests can stand in for the clock.
        self.sleep = sleep or (lambda seconds: _sleep_sync(seconds))

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        auth: str = "required",
        idempotency_key: str | None = None,
        retry: bool | None = None,
        timeout: float | None = None,
    ) -> Response:
        key = self._key(auth)
        url = f"{self.base_url}{path}"
        headers = self._headers(key, idempotency_key, json is not None)
        query = {k: v for k, v in (params or {}).items() if v is not None}
        attempts = 1 + (self.max_retries if (retry if retry is not None else method == "GET" or idempotency_key) else 0)
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = self.http.request(
                    method, url, json=json, params=query, headers=headers, timeout=timeout or self.timeout
                )
            except httpx.TransportError as error:
                if attempt < attempts:
                    self.sleep(backoff(attempt))
                    continue
                raise self._connection_error(url, error) from None
            result, failure = self._parse(method, path, response)
            _log(method, path, response, attempt, started)
            if result is not None:
                return result
            assert failure is not None
            if attempt < attempts and _retryable(failure):
                self.sleep(retry_delay(response.headers, failure, attempt))
                continue
            raise failure
        raise AssertionError("unreachable")

    def put(self, url: str, data: bytes, headers: Mapping[str, str]) -> None:
        """PUT bytes to a presigned storage URL. The API key is never sent there."""
        try:
            response = self.http.put(url, content=data, headers=dict(headers), timeout=TRANSFER_TIMEOUT)
        except httpx.TransportError as error:
            raise _upload_unreachable(error) from None
        if not response.is_success:
            raise APIError(
                f"Uploading failed: storage answered {response.status_code}.",
                hint="Upload URLs expire quickly; try again.",
                code="upload_failed",
                retryable=True,
            )

    def download(self, url: str, path: Path, attempts: int = 3) -> int:
        """Stream ``url`` to ``path`` through a ``.part`` file, so a failure never leaves half a file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        for attempt in range(1, attempts + 1):
            try:
                with self.http.stream(
                    "GET", url, headers={"user-agent": USER_AGENT}, timeout=TRANSFER_TIMEOUT, follow_redirects=True
                ) as response:
                    if response.status_code != 200:
                        retryable = response.status_code == 429 or response.status_code >= 500
                        raise _DownloadError(f"HTTP {response.status_code}", retryable)
                    with open(part, "wb") as out:
                        for chunk in response.iter_bytes():
                            out.write(chunk)
                os.replace(part, path)
                return path.stat().st_size
            except (httpx.TransportError, _DownloadError) as error:
                part.unlink(missing_ok=True)
                retryable = not isinstance(error, _DownloadError) or error.retryable
                if attempt < attempts and retryable:
                    self.sleep(backoff(attempt))
                    continue
                raise _download_failed(url, error) from None
        raise AssertionError("unreachable")

    def read(self, url: str) -> bytes:
        response = self.http.get(
            url, headers={"user-agent": USER_AGENT}, timeout=TRANSFER_TIMEOUT, follow_redirects=True
        )
        if response.status_code != 200:
            raise _download_failed(url, _DownloadError(f"HTTP {response.status_code}", False))
        return bytes(response.content)


class AsyncTransport(_Base):
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float,
        max_retries: int,
        http_client: httpx.AsyncClient | None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries, headers=headers)
        self._given_http = http_client
        # One connection pool per event loop: a pool made under one asyncio.run()
        # cannot be used under the next, and module-level helpers outlive loops.
        self._pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
            weakref.WeakKeyDictionary()
        )
        self.sleep = sleep or (lambda seconds: _sleep_async(seconds))

    @property
    def http(self) -> httpx.AsyncClient:
        if self._given_http is not None:
            return self._given_http
        loop = asyncio.get_running_loop()
        pool = self._pools.get(loop)
        if pool is None:
            pool = self._pools[loop] = httpx.AsyncClient(follow_redirects=True)
        return pool

    async def close(self) -> None:
        if self._given_http is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            pool = self._pools.pop(loop, None)
            if pool is not None:
                await pool.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        auth: str = "required",
        idempotency_key: str | None = None,
        retry: bool | None = None,
        timeout: float | None = None,
    ) -> Response:
        key = self._key(auth)
        url = f"{self.base_url}{path}"
        headers = self._headers(key, idempotency_key, json is not None)
        query = {k: v for k, v in (params or {}).items() if v is not None}
        attempts = 1 + (self.max_retries if (retry if retry is not None else method == "GET" or idempotency_key) else 0)
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = await self.http.request(
                    method, url, json=json, params=query, headers=headers, timeout=timeout or self.timeout
                )
            except httpx.TransportError as error:
                if attempt < attempts:
                    await self.sleep(backoff(attempt))
                    continue
                raise self._connection_error(url, error) from None
            result, failure = self._parse(method, path, response)
            _log(method, path, response, attempt, started)
            if result is not None:
                return result
            assert failure is not None
            if attempt < attempts and _retryable(failure):
                await self.sleep(retry_delay(response.headers, failure, attempt))
                continue
            raise failure
        raise AssertionError("unreachable")

    async def put(self, url: str, data: bytes, headers: Mapping[str, str]) -> None:
        try:
            response = await self.http.put(url, content=data, headers=dict(headers), timeout=TRANSFER_TIMEOUT)
        except httpx.TransportError as error:
            raise _upload_unreachable(error) from None
        if not response.is_success:
            raise APIError(
                f"Uploading failed: storage answered {response.status_code}.",
                hint="Upload URLs expire quickly; try again.",
                code="upload_failed",
                retryable=True,
            )

    async def download(self, url: str, path: Path, attempts: int = 3) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        for attempt in range(1, attempts + 1):
            try:
                async with self.http.stream(
                    "GET", url, headers={"user-agent": USER_AGENT}, timeout=TRANSFER_TIMEOUT, follow_redirects=True
                ) as response:
                    if response.status_code != 200:
                        retryable = response.status_code == 429 or response.status_code >= 500
                        raise _DownloadError(f"HTTP {response.status_code}", retryable)
                    with open(part, "wb") as out:
                        async for chunk in response.aiter_bytes():
                            out.write(chunk)
                os.replace(part, path)
                return path.stat().st_size
            except (httpx.TransportError, _DownloadError) as error:
                part.unlink(missing_ok=True)
                retryable = not isinstance(error, _DownloadError) or error.retryable
                if attempt < attempts and retryable:
                    await self.sleep(backoff(attempt))
                    continue
                raise _download_failed(url, error) from None
        raise AssertionError("unreachable")

    async def read(self, url: str) -> bytes:
        response = await self.http.get(
            url, headers={"user-agent": USER_AGENT}, timeout=TRANSFER_TIMEOUT, follow_redirects=True
        )
        if response.status_code != 200:
            raise _download_failed(url, _DownloadError(f"HTTP {response.status_code}", False))
        return bytes(response.content)


def _upload_unreachable(error: Exception) -> APIConnectionError:
    reason = "timed out" if isinstance(error, httpx.TimeoutException) else "the connection failed"
    return APIConnectionError(
        f"Uploading failed: {reason}.",
        hint="Check your connection and try again.",
        code="network_error",
        retryable=True,
    )


class _DownloadError(Exception):
    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _download_failed(url: str, error: Exception) -> SumeError:
    name = httpx.URL(url).path.rsplit("/", 1)[-1] or url
    reason = "timed out" if isinstance(error, httpx.TimeoutException) else str(error) or type(error).__name__
    return SumeError(
        f"Could not download {name} ({reason}).",
        hint=f"The file is still at {url}",
        code="download_failed",
        retryable=True,
    )
