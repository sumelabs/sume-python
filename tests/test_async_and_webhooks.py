from __future__ import annotations

import asyncio
import hashlib
import hmac
from pathlib import Path

import httpx
import pytest

import sume
from tests.conftest import KEY, PNG, MockApi


@pytest.mark.anyio
async def test_async_run_and_save(async_client: sume.AsyncClient, api: MockApi, tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(PNG)
    seen: list[str] = []

    async def note(status: sume.Status) -> None:
        seen.append(status.state)

    image = await async_client.run("gpt-image-2", "a kite", image="photo.png", quality="low", on_status=note)
    assert image.usd == "0.0077" and seen[-1] == "completed"
    assert await image.save("kite.png") == [tmp_path / "kite.png"]
    assert (tmp_path / "kite.png").read_bytes() == PNG


@pytest.mark.anyio
async def test_async_lifecycle(async_client: sume.AsyncClient, api: MockApi) -> None:
    api.polls_until_done = 2
    job = await async_client.submit("seedance-2-mini", "a boat", duration=4)
    states = [s.state async for s in job.poll()]
    assert states == ["queued", "completed"]
    result = await async_client.result(job.id)
    assert result.url and result.model == "seedance-2-mini"
    api.outcome = "failed"
    with pytest.raises(sume.JobFailedError, match="The provider timed out"):
        await async_client.run("gpt-image-2", "a kite")
    with pytest.raises(sume.OverBudgetError):
        await async_client.run("seedance-2-mini", "a boat", duration=15, max_usd=1)


def test_module_async_helpers_survive_separate_event_loops(api: MockApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUME_API_KEY", KEY)
    monkeypatch.setenv("SUME_API_BASE_URL", "https://api.test")
    transport = sume._default_async._transport
    monkeypatch.setattr(
        type(transport), "http", property(lambda self: httpx.AsyncClient(transport=httpx.MockTransport(api.handle)))
    )
    first = asyncio.run(sume.run_async("gpt-image-2", "a kite"))
    second = asyncio.run(sume.run_async("gpt-image-2", "another kite"))
    assert first.job_id != second.job_id


def sign(body: bytes, timestamp: int, secret: str) -> str:
    return "sume-v1=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def test_verify_webhook_matches_the_sume_v1_vector() -> None:
    # The same known answer @sume-com/sdk pins, so both verifiers agree with the signer.
    assert sign(b"hello", 1_700_000_000, "s3cret") == (
        "sume-v1=babfa8b593140ee3160bea38210f8fea6712e1f6e073328eb52c4e965bc0ab8d"
    )
    headers = {
        "X-Sume-Webhook-Timestamp": "1700000000",
        "X-Sume-Webhook-Signature": sign(b"hello", 1_700_000_000, "s3cret"),
    }
    assert sume.verify_webhook(b"hello", headers, "s3cret", now=1_700_000_000)
    assert sume.verify_webhook("hello", headers, "s3cret", now=1_700_000_000)
    assert not sume.verify_webhook(b"hello ", headers, "s3cret", now=1_700_000_000)
    assert not sume.verify_webhook(b"hello", headers, "other", now=1_700_000_000)
    assert not sume.verify_webhook(b"hello", headers, "s3cret", now=1_700_000_601)
    assert sume.verify_webhook(b"hello", headers, "s3cret", now=1_800_000_000, tolerance=0)
    rotating = {
        **headers,
        "X-Sume-Webhook-Signature": f"{sign(b'hello', 1_700_000_000, 'new')}, {headers['X-Sume-Webhook-Signature']}",
    }
    assert sume.verify_webhook(b"hello", rotating, "s3cret", now=1_700_000_000)
    assert sume.verify_webhook(b"hello", rotating, "new", now=1_700_000_000)
    assert not sume.verify_webhook(b"hello", {}, "s3cret")


@pytest.mark.anyio
async def test_async_field_errors_name_what_the_model_takes(async_client: sume.AsyncClient, api: MockApi) -> None:
    api.reject_fields = True
    with pytest.raises(sume.InvalidRequestError) as caught:
        await async_client.run("seedance-2-mini", "a boat", duration=40)
    assert [f.path for f in caught.value.field_errors] == ["duration", ""]
    assert "Fix: seedance-2-mini takes duration 4–15 s." in str(caught.value)
