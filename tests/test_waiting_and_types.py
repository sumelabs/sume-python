from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import sume
from tests.conftest import BASE, KEY, PNG, MockApi


def test_on_submit_and_on_event_report_progress(client: sume.Client, api: MockApi) -> None:
    api.polls_until_done = 2
    submitted: list[str] = []
    events: list[str] = []
    image = client.run(
        "gpt-image-2",
        "a kite",
        on_submit=lambda job: submitted.append(job.id),
        on_event=lambda event: events.append(event.type),
    )
    assert submitted == [image.job_id]
    assert events == ["job.created", "job.completed"]  # each event once, in order


def test_a_full_account_waits_for_a_slot_instead_of_failing(client: sume.Client, api: MockApi) -> None:
    api.slots_busy = 3
    image = client.run("gpt-image-2", "a kite")
    generates = api.calls("POST", "/v1/image-router/generate")
    assert len(generates) == 4 and len(api.jobs) == 1
    assert len({r.headers["idempotency-key"] for r in generates}) == 1  # the same submit, asked again
    assert image.url


def test_a_slot_that_never_frees_gives_up_with_the_reason(client: sume.Client, api: MockApi) -> None:
    api.slots_busy = 100
    with pytest.raises(sume.RateLimitError, match="Too many jobs in flight"):
        client.run("gpt-image-2", "a kite", timeout=5)


def test_cancel_on_timeout_refunds_a_job_that_never_started(client: sume.Client, api: MockApi) -> None:
    api.outcome = "running"
    with pytest.raises(sume.WaitTimeoutError) as caught:
        client.run("gpt-image-2", "a kite", timeout=0.001, cancel_on_timeout=True)
    assert "canceled job" in str(caught.value) and "refunded" in str(caught.value)
    assert api.jobs[caught.value.job_id or ""]["status"] == "canceled"


def test_field_errors_come_back_one_by_one(client: sume.Client, api: MockApi) -> None:
    api.reject_fields = True
    with pytest.raises(sume.InvalidRequestError) as caught:
        client.run("seedance-2-mini", "a boat", duration=40)
    assert [(e.path, e.message) for e in caught.value.field_errors] == [
        ("duration", "must be between 4 and 15"),
        ("", "bad"),
    ]
    assert "  - duration: must be between 4 and 15" in str(caught.value)
    assert str(caught.value.field_errors[0]) == "duration: must be between 4 and 15"
    # The fix names what the model takes, from the catalog.
    assert 'Fix: seedance-2-mini takes duration 4–15 s. See all its fields with sume.model("seedance-2-mini").' in str(
        caught.value
    )


def test_file_objects_are_uploaded_wherever_they_are(client: sume.Client, api: MockApi, tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(PNG)
    client.run(
        "seedance-2-mini",
        "slow push in",
        duration=4,
        input_references=[{"type": "image_url", "image_url": {"url": Path("photo.png")}}],
        frame_images=[{"type": "image_url", "image_url": {"url": "photo.png"}, "frame_type": "first_frame"}],
        metadata={"note": "photo.png"},  # text, not a file: left alone
    )
    [body] = [json.loads(r.content) for r in api.calls("POST", "/v1/video-router/generate")]
    assert body["input_references"][0]["image_url"]["url"].startswith("https://media.test/assets/")
    # A path string where a media URL belongs (a nested image_url.url) is a file too.
    assert body["frame_images"][0]["image_url"]["url"].startswith("https://media.test/assets/")
    assert body["frame_images"][0]["type"] == "image_url"
    assert body["metadata"] == {"note": "photo.png"}


def test_known_models_need_no_catalog_lookup(client: sume.Client, api: MockApi) -> None:
    client.run("gpt-image-2", "a kite")
    assert api.calls("GET", "/v1/open/catalog") == []
    with pytest.raises(sume.NotFoundError, match="Did you mean gpt-image-2"):
        client.run("gpt-image-2x", "a kite")


def test_result_carries_the_job_and_converts_to_json(client: sume.Client, api: MockApi) -> None:
    image = client.run("gpt-image-2", "a kite", quality="low")
    assert image.job is not None and image.job.seconds == 15.5
    assert image.job.input["quality"] == "low"
    summary = image.to_dict()
    assert summary["url"] == image.url and summary["usd"] == "0.0077" and summary["seconds"] == 15.5
    assert summary["files"][0]["content_type"] == "image/png"
    json.dumps(summary)


def test_extra_headers_go_to_the_api_only(api: MockApi) -> None:
    client = sume.Client(
        api_key=KEY,
        base_url=BASE,
        headers={"X-Trace-Id": "abc"},
        http_client=httpx.Client(transport=httpx.MockTransport(api.handle)),
    )
    client.run("gpt-image-2", "a kite").save()
    api_calls = [r for r in api.requests if r.url.host == "api.test"]
    other = [r for r in api.requests if r.url.host != "api.test"]
    assert all(r.headers.get("x-trace-id") == "abc" for r in api_calls)
    assert other and not any("x-trace-id" in r.headers for r in other)


def test_input_passes_fields_the_types_do_not_list(client: sume.Client, api: MockApi) -> None:
    client.run("gpt-image-2", "a kite", input={"future_field": 1}, quality="low")
    [body] = [json.loads(r.content) for r in api.calls("POST", "/v1/image-router/generate")]
    assert body["future_field"] == 1 and body["quality"] == "low"


def test_the_generated_types_cover_the_catalog() -> None:
    from typing import get_args

    from sume._generated import MODEL_KINDS

    ids = set(get_args(sume.ModelId))
    assert "gpt-image-2" in ids and "seedance-2-mini" in ids
    assert ids == set(MODEL_KINDS)
    assert set(get_args(sume.ImageModelId)) | set(get_args(sume.VideoModelId)) == ids
    assert {"prompt", "image", "duration", "captions", "quality"} <= set(sume.Inputs.__annotations__)


def test_the_prompt_by_position_wins_over_prompt_by_name(client: sume.Client, api: MockApi) -> None:
    client.run("gpt-image-2", "by position", prompt="by name")
    client.run("gpt-image-2", prompt="only by name")
    bodies = [json.loads(r.content) for r in api.calls("POST", "/v1/image-router/generate")]
    assert [b["prompt"] for b in bodies] == ["by position", "only by name"]
