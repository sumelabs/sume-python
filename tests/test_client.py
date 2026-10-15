from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import sume
from tests.conftest import BASE, KEY, PNG, MockApi


def bodies(api: MockApi, path: str) -> list[dict]:
    return [json.loads(r.content) for r in api.calls("POST", path)]


def test_run_returns_files_and_the_price(client: sume.Client, api: MockApi) -> None:
    image = client.run("gpt-image-2", "a red kite over a beach", quality="low")
    assert image.model == "gpt-image-2"
    assert image.usd == "0.0077"
    assert image.url == f"https://media.test/{image.job_id}/1.png"
    assert len(image.files) == 1
    assert bodies(api, "/v1/image-router/generate") == [
        {"prompt": "a red kite over a beach", "quality": "low", "model": "gpt-image-2"}
    ]
    # Every submit carries an idempotency key, so a retried submit never pays twice.
    assert api.calls("POST", "/v1/image-router/generate")[0].headers["idempotency-key"]


def test_save_names_files_and_never_overwrites(client: sume.Client, api: MockApi, tmp_path: Path) -> None:
    image = client.run("gpt-image-2", "a kite")
    [first] = image.save()
    assert first.name == f"gpt-image-2-{image.job_id[4:12]}.png"
    assert first.read_bytes() == PNG
    [second] = image.save()
    assert second.name.endswith("-2.png") and second != first
    assert image.save("kite.png") == [tmp_path / "kite.png"]
    assert image.save("out/") == [tmp_path / "out" / first.name]
    assert not list(tmp_path.glob("**/*.part"))


def test_several_files_are_numbered_and_a_poster_is_left_out(client: sume.Client, api: MockApi, tmp_path: Path) -> None:
    api.artifacts = "two-images"
    images = client.run("gpt-image-2", "two kites", num_images=2)
    assert [p.name for p in images.save("kite.png")] == ["kite.png", "kite-2.png"]
    api.artifacts = "video-with-poster"
    video = client.run("seedance-2-mini", "a paper boat", duration=4, resolution="480p")
    assert [f.type for f in video.files] == ["video"]
    assert [f.type for f in video.all_files] == ["video", "image"]
    assert video.save("boat.mp4") == [tmp_path / "boat.mp4"]
    assert video.usd == "0.3516"


def test_local_files_are_uploaded_without_the_key(client: sume.Client, api: MockApi, tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(PNG)
    client.run("gpt-image-2", "the same cup at night", image="photo.png")
    [generate] = bodies(api, "/v1/image-router/generate")
    assert generate["image_urls"] == ["https://media.test/assets/asset_1/source.png"]
    assert api.uploads["asset_1"] == PNG
    [create] = bodies(api, "/v1/assets/upload-url")
    assert create == {"content_type": "image/png", "size_bytes": len(PNG), "filename": "photo.png"}
    # The mock fails any request that carries the key to storage or media.


def test_bytes_and_video_short_names_map_to_the_video_router(client: sume.Client, api: MockApi) -> None:
    client.run("seedance-2-mini", "slow push in", image=PNG, duration=4)
    [generate] = bodies(api, "/v1/video-router/generate")
    assert generate["image_url"].startswith("https://media.test/assets/")
    with pytest.raises(sume.InvalidRequestError, match="takes one image"):
        client.run("seedance-2-mini", "x", image=[PNG, PNG], duration=4)
    with pytest.raises(sume.InvalidRequestError, match="video= applies to video models"):
        client.run("gpt-image-2", "x", video="https://example.com/a.mp4")
    with pytest.raises(sume.InvalidRequestError, match="needs duration="):
        client.run("veed-lipsync", video="https://example.com/a.mp4", audio="https://example.com/a.wav")


def test_max_usd_refuses_before_anything_is_uploaded_or_submitted(client: sume.Client, api: MockApi) -> None:
    with pytest.raises(sume.OverBudgetError) as caught:
        client.run("seedance-2-mini", "a boat", duration=10, image=PNG, max_usd=0.50)
    assert "costs $0.8790, over max_usd=$0.5" in str(caught.value)
    assert api.calls("POST", "/v1/video-router/generate") == []
    assert api.calls("POST", "/v1/assets/upload-url") == []
    assert client.run("gpt-image-2", "a kite", max_usd="0.01").usd == "0.0077"


def test_a_failed_job_raises_with_the_reason(client: sume.Client, api: MockApi) -> None:
    api.outcome = "failed"
    with pytest.raises(sume.JobFailedError) as caught:
        client.run("gpt-image-2", "a kite")
    assert "failed: The provider timed out." in str(caught.value)
    assert "refunded" in str(caught.value)
    assert caught.value.job_id and caught.value.job_id.startswith("job_")


def test_a_job_refused_at_submit_is_a_failed_job(client: sume.Client, api: MockApi) -> None:
    api.outcome = "refused"
    with pytest.raises(sume.JobFailedError, match="The provider refused this prompt") as caught:
        client.run("gpt-image-2", "something refused")
    assert caught.value.job_id


def test_errors_say_the_fix(client: sume.Client, api: MockApi) -> None:
    with pytest.raises(sume.NotFoundError) as typo:
        client.run("gpt-image2", "a kite")
    assert "Fix: Did you mean gpt-image-2?" in str(typo.value)

    api.balance_usd = 0
    with pytest.raises(sume.InsufficientFundsError) as broke:
        client.run("gpt-image-2", "a kite")
    assert "Top up at https://open.sume.com/billing. It needs $0.0077; the balance is $0.00." in str(broke.value)
    assert broke.value.request_id == "req_test"

    wrong = sume.Client(
        api_key="sume_live_wrong", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(api.handle))
    )
    with pytest.raises(sume.AuthenticationError) as denied:
        wrong.list_jobs()
    assert "A key works only on the API it was made for" in str(denied.value)


def test_the_key_comes_from_the_environment_then_dotenv(
    api: MockApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def make() -> sume.Client:
        return sume.Client(base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(api.handle)))

    with pytest.raises(sume.AuthenticationError) as missing:
        make().list_jobs()
    assert "Fix: Set SUME_API_KEY" in str(missing.value)
    assert "https://open.sume.com/api-keys" in str(missing.value)

    (tmp_path / ".env").write_text(f'OTHER=1\nexport SUME_API_KEY="{KEY}"\nSUME_API_BASE_URL=https://evil.example\n')
    assert make().list_jobs() == []
    # The host never comes from .env.
    assert make().base_url == BASE
    assert api.requests[-1].url.host == "api.test"

    monkeypatch.setenv("SUME_API_KEY", "sume_live_from_env")
    with pytest.raises(sume.AuthenticationError):
        make().list_jobs()  # the environment wins over .env


def test_the_default_host_is_open_production(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sume.Client().base_url == "https://api.open.sume.com"
    assert sume.Client(base_url="https://api.open.sume.com/v1/").base_url == "https://api.open.sume.com"
    monkeypatch.setenv("SUME_API_BASE_URL", "https://open-api-development.up.railway.app")
    assert sume.Client().base_url == "https://open-api-development.up.railway.app"
    with pytest.raises(sume.SumeError, match="must use https"):
        sume.Client(base_url="http://api.open.sume.com")
    assert sume.Client(base_url="http://localhost:8080").base_url == "http://localhost:8080"
    with pytest.raises(sume.AuthenticationError, match="characters a key never has"):
        sume.Client(api_key="sume_live_abc\ndef").list_jobs()


def test_submit_status_wait_cancel_events(client: sume.Client, api: MockApi) -> None:
    api.polls_until_done = 3
    job = client.submit("gpt-image-2", "a kite")
    assert job.model == "gpt-image-2" and job.idempotency_key
    assert client.status(job.id).state == "queued"
    seen: list[str] = []
    result = job.wait(on_status=lambda s: seen.append(str(s)))
    assert seen[-1] == "completed" and seen[0].startswith("queued")
    assert result.url and result.usd is None  # submit does not price unless asked
    assert [e.type for e in job.events()] == ["job.created", "job.completed"]
    assert job.info().state == "completed"
    assert client.result(job.id).url == result.url

    api.outcome = "running"
    queued = client.submit("gpt-image-2", "another kite")
    assert queued.cancel() is True
    with pytest.raises(sume.JobFailedError, match="was canceled"):
        queued.wait()
    with pytest.raises(sume.InvalidRequestError, match="Generation has started"):
        client.cancel(job.id)
    assert [j.id for j in client.list_jobs()][:2] == [queued.id, job.id]


def test_a_wait_that_runs_out_names_the_job(client: sume.Client, api: MockApi) -> None:
    api.outcome = "running"
    with pytest.raises(sume.WaitTimeoutError) as caught:
        client.run("gpt-image-2", "a kite", timeout=0.001)
    assert caught.value.job_id
    assert f'sume.result("{caught.value.job_id}")' in str(caught.value)


def test_retries_reuse_the_idempotency_key(client: sume.Client, api: MockApi) -> None:
    api.fail_next = [503]  # the catalog read
    client.model("gpt-image-2")
    api.fail_next = [503, 502]  # the price, then the submit
    result = client.run("gpt-image-2", "a kite")
    keys = {r.headers["idempotency-key"] for r in api.calls("POST", "/v1/image-router/generate")}
    assert len(keys) == 1 and len(api.jobs) == 1
    assert result.url


def test_a_retried_submit_with_the_same_key_adopts_the_job(client: sume.Client, api: MockApi) -> None:
    first = client.submit("gpt-image-2", "a kite", idempotency_key="order-42")
    again = client.submit("gpt-image-2", "a kite", idempotency_key="order-42")
    assert again.id == first.id and again.replayed and not first.replayed


def test_models_and_estimate_need_no_key(api: MockApi) -> None:
    anonymous = sume.Client(base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(api.handle)))
    videos = anonymous.models("video")
    assert [m.id for m in videos] == ["seedance-2-mini"]
    assert videos[0].price == "$0.0879–$0.1890/s"
    assert videos[0].seconds == (4, 15)
    assert str(anonymous.estimate("seedance-2-mini", "a boat", duration=6)) == "$0.5274"
    assert anonymous.estimate("gpt-image-2", "x", image="photo.png").usd == "0.0077"  # not uploaded to price it
    assert api.calls("POST", "/v1/assets/upload-url") == []


def test_upload_returns_a_url(client: sume.Client, api: MockApi, tmp_path: Path) -> None:
    (tmp_path / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8)
    assert client.upload("clip.mp4").startswith("https://media.test/assets/")
    with open(tmp_path / "clip.mp4", "rb") as handle:
        assert client.upload(handle).startswith("https://media.test/assets/")
    with pytest.raises(sume.InvalidRequestError, match="No such file"):
        client.upload("missing.png")
    with pytest.raises(sume.InvalidRequestError, match="Cannot tell the media type"):
        client.upload(b"plain text")


def test_module_functions_use_the_environment(api: MockApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sume._default._transport, "http", httpx.Client(transport=httpx.MockTransport(api.handle)))
    monkeypatch.setenv("SUME_API_KEY", KEY)
    monkeypatch.setenv("SUME_API_BASE_URL", BASE)
    image = sume.run("gpt-image-2", "a kite", quality="low")
    assert image.usd == "0.0077"
    assert sume.status(image.job_id).ok
