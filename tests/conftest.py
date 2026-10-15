"""A mock Sume Open API on httpx.MockTransport. Tests never call the real API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

import sume

KEY = "sume_live_test0000_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE = "https://api.test"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64

MODELS: list[dict[str, Any]] = [
    {
        "id": "gpt-image-2",
        "slug": "openai/gpt-image-2",
        "name": "GPT Image 2",
        "kind": "image",
        "company": {"slug": "openai", "name": "OpenAI"},
        "modes": ["text-to-image", "image-edit"],
        "pricing": {
            "currency": "USD",
            "unit": "image",
            "from_usd": "0.0077",
            "from_usd_micros": 7700,
            "to_usd": "0.2639",
        },
        "capabilities": {"text_to_image": True, "image_edit": True, "qualities": ["low", "medium", "high"]},
    },
    {
        "id": "seedance-2-mini",
        "slug": "bytedance/seedance-2-mini",
        "name": "Seedance 2.0 Mini",
        "kind": "video",
        "company": {"slug": "bytedance", "name": "ByteDance"},
        "modes": ["text-to-video", "image-to-video"],
        "pricing": {
            "currency": "USD",
            "unit": "second",
            "from_usd": "0.0879",
            "from_usd_micros": 87900,
            "to_usd": "0.1890",
        },
        "capabilities": {"resolutions": ["480p", "720p"], "duration_seconds": {"min": 4, "max": 15}},
    },
    {
        "id": "veed-lipsync",
        "slug": "veed/veed-lipsync",
        "name": "VEED Lipsync",
        "kind": "lipsync",
        "company": {"slug": "veed", "name": "VEED"},
        "modes": ["video-to-lipsync"],
        "pricing": {"currency": "USD", "unit": "second", "from_usd": "0.1000", "from_usd_micros": 100000},
        "capabilities": {"duration_seconds": {"min": 2, "max": 60}},
    },
]


@dataclass
class MockApi:
    balance_usd: float = 100.0
    outcome: str = "completed"
    """completed, failed, refused (failed at submit), or running (never ends)."""
    polls_until_done: int = 1
    artifacts: str = "image"
    """image, two-images, or video-with-poster."""
    fail_next: list[int] = field(default_factory=list)
    """Status codes to answer the next API calls with, first in first out."""
    requests: list[httpx.Request] = field(default_factory=list)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    polls: dict[str, int] = field(default_factory=dict)
    uploads: dict[str, bytes] = field(default_factory=dict)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.startswith(path)]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host in ("storage.test", "media.test"):
            # Storage and media hosts must never see the API key.
            assert "authorization" not in request.headers, f"key sent to {host}"
            if host == "storage.test" and request.method == "PUT":
                self.uploads[request.url.path.rsplit("/", 1)[-1]] = request.content
                return httpx.Response(200)
            if host == "media.test":
                return httpx.Response(200, content=MP4 if request.url.path.endswith(".mp4") else PNG)
            return httpx.Response(404)
        assert host == "api.test"
        if self.fail_next:
            status = self.fail_next.pop(0)
            return self.error(status, f"http_{status}", f"Injected {status}.", retryable=True)
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        public = path.startswith("/v1/open/")
        auth = request.headers.get("authorization")
        if not public:
            if auth is None:
                return self.error(401, "unauthorized", "Missing or invalid API key.")
            if auth != f"Bearer {KEY}":
                return self.error(401, "unauthorized", "Missing or invalid API key.")

        if request.method == "GET" and path == "/v1/open/catalog":
            return self.ok({"currency": "USD", "models": MODELS})
        if request.method == "GET" and path.startswith("/v1/open/catalog/"):
            found = next((m for m in MODELS if m["id"] == path.rsplit("/", 1)[-1]), None)
            return self.ok(found) if found else self.error(404, "model_not_found", "No such model.")
        if request.method == "POST" and path == "/v1/open/estimate":
            model = next(m for m in MODELS if m["id"] == body["model"])
            usd = 0.0077 if model["kind"] == "image" else 0.0879 * max(int(body["input"].get("duration") or 4), 1)
            return self.ok(
                {"model": model["id"], "currency": "USD", "usd": f"{usd:.4f}", "usd_micros": round(usd * 1e6)}
            )
        if request.method == "POST" and path.endswith("-router/generate"):
            return self.generate(request, path, body)
        if request.method == "GET" and path.startswith("/v1/jobs/") and path.endswith("/status"):
            return self.status(path.split("/")[3])
        if request.method == "GET" and path.startswith("/v1/jobs/") and path.endswith("/result"):
            job = self.jobs[path.split("/")[3]]
            return self.ok({"job_id": job["id"], "status": "COMPLETED", "result": job["result"], "job": job})
        if request.method == "GET" and path.startswith("/v1/jobs/") and path.endswith("/events"):
            job = self.jobs[path.split("/")[3]]
            events = [{"id": "evt_1", "type": "job.created", "created_at": job["created_at"]}]
            if job["status"] in ("completed", "failed"):
                events.append({"id": "evt_2", "type": f"job.{job['status']}", "created_at": job["created_at"]})
            return self.ok({"job_id": job["id"], "events": events})
        if request.method == "POST" and path.startswith("/v1/jobs/") and path.endswith("/cancel"):
            job = self.jobs[path.split("/")[3]]
            if job["status"] != "queued":
                return self.error(
                    409, "job_generation_already_started", "API job can only be canceled before generation starts."
                )
            job["status"] = "canceled"
            return self.ok({"job_id": job["id"], "status": "CANCELED", "canceled": True, "job": job})
        if request.method == "GET" and path.startswith("/v1/jobs/"):
            job = self.jobs.get(path.split("/")[3])
            return self.ok({"job": job}) if job else self.error(404, "job_not_found", "No such job.")
        if request.method == "GET" and path == "/v1/jobs":
            return self.ok({"jobs": list(self.jobs.values())[::-1]})
        if request.method == "POST" and path == "/v1/assets/upload-url":
            asset_id = f"asset_{len(self.uploads) + 1}"
            return self.ok(
                {
                    "asset": {"id": asset_id, "media_type": "image", "status": "pending", "url": None},
                    "upload": {
                        "url": f"https://storage.test/put/{asset_id}",
                        "method": "PUT",
                        "headers": {"content-type": body["content_type"], "content-length": str(body["size_bytes"])},
                    },
                }
            )
        if request.method == "POST" and path.startswith("/v1/assets/") and path.endswith("/complete"):
            asset_id = path.split("/")[3]
            assert asset_id in self.uploads, "completed before the bytes were uploaded"
            return self.ok(
                {
                    "asset": {
                        "id": asset_id,
                        "status": "ready",
                        "url": f"https://media.test/assets/{asset_id}/source.png",
                    }
                }
            )
        return self.error(404, "not_found", f"No route {request.method} {path}.")

    def generate(self, request: httpx.Request, path: str, body: dict[str, Any]) -> httpx.Response:
        model = next((m for m in MODELS if m["id"] == body.get("model")), None)
        if model is None:
            return self.error(404, "model_not_found", "No such model.")
        router = "image" if model["kind"] == "image" else "video"
        if f"/v1/{router}-router/" not in path:
            return self.error(400, "invalid_request", f"{model['id']} is not a {router} model.")
        key = request.headers.get("idempotency-key")
        if key and key in self.by_key:
            job = self.jobs[self.by_key[key]]
            return self.ok({"job": job, "idempotency_hit": True, "next_poll_after_seconds": 0, "terminal": False}, 202)
        price = 0.0077 if model["kind"] == "image" else 0.0879 * int(body.get("duration") or 4)
        if price > self.balance_usd:
            return self.error(
                402,
                "insufficient_funds",
                "Balance too low.",
                details={"required_usd": f"{price:.4f}", "available_usd": "0.00"},
            )
        job_id = f"job_{len(self.jobs) + 1:08d}-0000-0000-0000-000000000000"
        job = {
            "id": job_id,
            "type": f"{router}_generation",
            "status": "queued",
            "model": model["id"],
            "idempotency_key": key,
            "created_at": "2026-10-15T00:00:00.000Z",
            "completed_at": None,
            "input": {k: v for k, v in body.items() if k != "model"},
            "result": None,
            "error": None,
        }
        self.jobs[job_id] = job
        if key:
            self.by_key[key] = job_id
        if self.outcome == "refused":
            job["status"] = "failed"
            job["error"] = {"code": "provider_refused", "message": "The provider refused this prompt."}
            return self.error(422, "provider_refused", "The provider refused this prompt.", details={"job": job})
        return self.ok({"job": job, "idempotency_hit": False, "next_poll_after_seconds": 0, "terminal": False}, 202)

    def status(self, job_id: str) -> httpx.Response:
        job = self.jobs[job_id]
        self.polls[job_id] = self.polls.get(job_id, 0) + 1
        if job["status"] == "queued" and self.outcome != "running" and self.polls[job_id] >= self.polls_until_done:
            if self.outcome == "failed":
                job["status"] = "failed"
                job["error"] = {"code": "provider_error", "message": "The provider timed out."}
            else:
                job["status"] = "completed"
                job["result"] = {"artifacts": self.make_artifacts(job_id)}
        state = job["status"]
        return self.ok(
            {
                "job_id": job_id,
                "status": state.upper(),
                "sume_status": state,
                "queue_position": 2 if state == "queued" else None,
                "terminal": state in ("completed", "failed", "canceled"),
                "next_poll_after_seconds": None if state != "queued" else 1,
                "job": job,
            }
        )

    def make_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        if self.artifacts == "video-with-poster":
            return [
                {
                    "id": "a1",
                    "type": "video",
                    "url": f"https://media.test/{job_id}/video.mp4",
                    "content_type": "video/mp4",
                },
                {
                    "id": "a2",
                    "type": "image",
                    "url": f"https://media.test/{job_id}/poster.jpg",
                    "content_type": "image/jpeg",
                },
            ]
        count = 2 if self.artifacts == "two-images" else 1
        return [
            {"id": f"a{i}", "type": "image", "url": f"https://media.test/{job_id}/{i}.png", "content_type": "image/png"}
            for i in range(1, count + 1)
        ]

    @staticmethod
    def ok(data: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"data": data}, headers={"x-sume-request-id": "req_test"})

    @staticmethod
    def error(status: int, code: str, message: str, *, retryable: bool = False, details: Any = None) -> httpx.Response:
        error: dict[str, Any] = {"code": code, "message": message, "request_id": "req_test", "retryable": retryable}
        if details is not None:
            error["details"] = details
        return httpx.Response(status, json={"error": error}, headers={"x-sume-request-id": "req_test"})


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Skip real sleeps, keep the environment clean, and run in a temp directory."""

    monkeypatch.setattr(sume._http, "_sleep_sync", lambda _seconds: None)
    monkeypatch.setattr(sume._http, "_sleep_async", lambda _seconds: _noop())
    monkeypatch.delenv("SUME_API_KEY", raising=False)
    monkeypatch.delenv("SUME_API_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)


async def _noop() -> None:
    return None


@pytest.fixture
def api() -> MockApi:
    return MockApi()


@pytest.fixture
def client(api: MockApi) -> sume.Client:
    return sume.Client(api_key=KEY, base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(api.handle)))


@pytest.fixture
def async_client(api: MockApi) -> sume.AsyncClient:
    return sume.AsyncClient(
        api_key=KEY, base_url=BASE, http_client=httpx.AsyncClient(transport=httpx.MockTransport(api.handle))
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
