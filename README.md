# sume

The official Python client for the [Sume Open API](https://open.sume.com). Run any open.sume.com model for images, video, lip-sync or captions, and save the file. Prices are in dollars. Sync and async, with types for every model and field.

```sh
pip install https://github.com/sumelabs/sume-python/releases/latest/download/sume.tar.gz
export SUME_API_KEY="sume_live_..."   # from https://open.sume.com/api-keys
```

```python
import sume

image = sume.run("gpt-image-2", "a red kite over a beach", quality="low")
image.save("kite.png")
```

That is the whole program. `run` prices the request and submits it, uploading any local files first. It waits for the job and returns the files; `save` writes them to disk. This one cost $0.0060.

- [Install](#install)
- [Key](#key)
- [Run a model](#run-a-model)
- [Types](#types)
- [Pick a model](#pick-a-model)
- [Progress, long jobs and cancel](#progress-long-jobs-and-cancel)
- [Files](#files)
- [Async](#async)
- [In a web app](#in-a-web-app)
- [Webhooks](#webhooks)
- [Errors](#errors)
- [Configuration](#configuration)
- [Coming from fal or Higgsfield](#coming-from-fal-or-higgsfield)
- [Reference](#reference)

## Install

```sh
pip install https://github.com/sumelabs/sume-python/releases/latest/download/sume.tar.gz
```

Python 3.9 or newer. The dependencies are `httpx` and `typing-extensions`. The package is not on PyPI yet. Once it is, the install line will be `pip install sume`.

## Key

A person makes the key at [open.sume.com → API keys](https://open.sume.com/api-keys). The client reads it from `SUME_API_KEY`, or from a `SUME_API_KEY=` line in `./.env`:

```sh
export SUME_API_KEY="sume_live_..."
```

Nothing else needs setting up. The client calls `https://api.open.sume.com` unless you tell it otherwise (see [Configuration](#configuration)). Keep the key on the server: anyone holding it can spend your balance.

## Run a model

```python
import sume

# Text to image
image = sume.run("gpt-image-2", "a ceramic cup on a wooden table, morning light", quality="low")
print(image.url, image.usd)  # https://…/….png 0.0060

# Edit an image: local files are uploaded first
night = sume.run("gpt-image-2", "the same cup at night, candlelight", image="cup.png")

# Image to video, with a budget
video = sume.run(
    "grok-imagine-video-1.5", "slow push in, steam rising", image="cup.png", duration=4, resolution="480p", max_usd=0.50
)
video.save("cup.mp4")

# Lip-sync a clip to speech
talk = sume.run("veed-lipsync", video="talk.mp4", audio="line.wav", duration=6)

# Burn captions into a clip
captioned = sume.run("veed-subtitles", video="clip.mp4", duration=12, captions={"style": "glass", "position": "bottom"})
```

- **Inputs** use the API's own field names: `quality`, `duration`, `resolution`, `aspect_ratio`, `num_images`, `output_format`, `captions`, and so on. Your editor completes them (see [Types](#types)), and `sume.model(id)` says what one model takes.
- **Media** has short names: `image=`, `video=`, `audio=`, `end_image=`, `reference_images=` and `mask=`. Each value can be an https URL, a file path, bytes, an open binary file or a PIL image. Anything that is not a URL is uploaded first.
- **`max_usd`** refuses to submit a request that costs more. It raises `OverBudgetError` before anything is uploaded or charged.
- **`timeout`** is how long `run` waits in all, in seconds: 30 minutes by default. When it passes, the job keeps running; see [Progress, long jobs and cancel](#progress-long-jobs-and-cancel).
- **Retries never pay twice.** Every submit carries an idempotency key, and the client's retries reuse it. To retry a submit yourself, pass `idempotency_key=`.
- **A busy account waits.** When the account already runs as many jobs as it may, `run` and `submit` wait for a free slot and submit then, instead of failing.

`run` returns a `Result`:

| | |
|---|---|
| `result.url`, `result.urls` | The file URLs |
| `result.files` | `File` objects with `url`, `type`, `content_type`, `width`, `height`, `duration_ms`, plus `save()` and `read()` |
| `result.save(path=None)` | Downloads every file and returns the paths. A file name: the first file takes it and the rest are numbered. A directory (ending in `/`): the files go inside it. No path: `<model>-<job>.<ext>` in the working directory, never overwriting a file |
| `result.usd` | The price in dollars, as a string like `"0.0060"` |
| `result.job` | The job: `input`, `created_at`, `completed_at`, and `seconds` (how long it ran) |
| `result.to_dict()` | A JSON-ready summary, to return from a web handler |
| `result.job_id`, `result.model` | Which job made it |

A video's poster frame is left out of `files`; `all_files` has every file the job made.

## Types

Every model id and request field is typed, with the API's own descriptions, so an editor completes them and a type checker catches mistakes before they cost anything:

```python
sume.run("gpt-image-2", "a red kite", qualty="low")
sume.run("seedance-2-mini", "a paper boat", duration="5")
```

```text
$ mypy app.py
app.py:1: error: Unexpected keyword argument "qualty"; did you mean "quality"?  [call-arg]
app.py:2: error: Argument "duration" has incompatible type "str"; expected "int"  [arg-type]
```

- `sume.ModelId` lists every model this version knows; `sume.ImageModelId` and `sume.VideoModelId` split them by kind. A model added to the catalog later still works: `run` takes any id as a string.
- `sume.Inputs` is the `TypedDict` of request fields that `run`, `submit` and `estimate` take as keyword arguments. Values with a fixed set, like `aspect_ratio`, `resolution` and `captions["position"]`, are `Literal`s.
- A field newer than this version goes through `input=`, as a plain dict: `sume.run("gpt-image-2", "a kite", input={"new_field": 1})`.
- What comes back is typed too: `Result`, `Job`, `Status`, `Event`, `Model`, `Price` and `JobInfo` are classes with typed attributes, not dicts. Each of them except `Job` keeps the API's own JSON in `raw`.

The types come from the live API: `python scripts/generate.py` rewrites them, and a weekly check fails when the API moves on.

## Pick a model

Prices differ by more than 100× between models, so name the model yourself. Listing and pricing are free, and need no key:

```python
for m in sume.models("video"):  # image, video, lipsync or captions
    print(m.id, m.price, m.seconds)  # seedance-2-mini $0.02936–$0.06352/s (4, 15)

sume.model("seedance-2-mini")  # one model: inputs, resolutions, limits, price
sume.estimate("seedance-2-mini", "a paper boat", duration=8, resolution="720p")  # $0.5082
```

## Progress, long jobs and cancel

`run` takes callbacks for progress:

```python
video = sume.run(
    "seedance-2-mini",
    "a paper boat on a pond",
    duration=5,
    resolution="480p",
    on_submit=lambda job: print("submitted", job.id),  # keep the id: it is all you need later
    on_status=lambda status: print(status),  # queued (#2 in line), processing, completed
    on_event=lambda event: print(event.type),  # job.created, job.started, job.completed
)
```

Video can take minutes. Submit now and collect later:

```python
job = sume.submit("kling-3", "a red kite over a beach", duration=5)
print(job.id)

job.status()  # one read: queued, processing, completed, failed or canceled
for status in job.poll():  # every read until the job ends
    print(status)
video = job.wait()  # the Result, or JobFailedError

# Later, anywhere, with only the id:
video = sume.result("job_7513b52d-3669-4ffe-b317-8b02000715eb")
job.events()  # the job's life: job.created, job.started, job.completed…
sume.list_jobs(limit=10)  # newest first
```

A job that has not started generating can be canceled, and it is refunded:

```python
sume.cancel(job.id)  # True when canceled

# Or give up on a wait, and cancel the job if it is still queued:
sume.run("kling-3", "a red kite", duration=5, timeout=120, cancel_on_timeout=True)
```

`wait`, `poll` and `run` read the status as often as the API asks: at most every 5 s for the first minute, every 10 s after that, and every 15 s after ten minutes. Network errors and errors on Sume's side do not end the wait. When `timeout` passes, `WaitTimeoutError` names the job, and the job keeps running (unless you passed `cancel_on_timeout=True` and it had not started). Do not submit again: a second submit is a second charge.

## Files

Pass a local file wherever a URL goes, and it is uploaded first: as a short name (`image="cup.png"`), in a media field (`image_urls=["a.png", "b.png"]`), or nested, as in `frame_images` or `input_references`:

```python
from pathlib import Path

sume.run(
    "seedance-2",
    "the two of them walk into the sea",
    duration=5,
    input_references=[{"type": "image_url", "image_url": {"url": Path("her.png")}}],
)
```

A file can be a path, bytes, an open binary file, or a PIL image. A plain string counts as a path only where a media URL belongs, so a prompt or `metadata` is never mistaken for a file. To get a URL first, for reuse:

```python
url = sume.upload("photo.png")
url = sume.upload(png_bytes, content_type="image/png")
```

Downloads stream to a `.part` file and are renamed when complete, so an interrupted download never leaves half a file behind. The API key is sent to the API only, never to the storage or media hosts.

## Async

Every call has an async form, on `sume.AsyncClient` or as a module function:

```python
import asyncio
import sume


async def main():
    image = await sume.run_async("gpt-image-2", "a red kite over a beach", quality="low")
    await image.save("kite.png")


asyncio.run(main())
```

Run several at once:

```python
async with sume.AsyncClient() as client:
    prompts = ["a red kite", "a blue kite", "a green kite"]
    images = await asyncio.gather(*(client.run("gpt-image-2", p, quality="low") for p in prompts))
```

The module has `run_async`, `submit_async`, `status_async`, `result_async`, `cancel_async`, `estimate_async`, `models_async`, `model_async`, `upload_async` and `list_jobs_async`. Async callbacks (`on_status=`, `on_event=`, `on_submit=`) may be plain functions or coroutines.

## In a web app

Keep one client and return `result.to_dict()`. With FastAPI:

```python
from fastapi import FastAPI
import sume

app = FastAPI()
client = sume.AsyncClient()


@app.post("/images")
async def make_image(prompt: str):
    image = await client.run("gpt-image-2", prompt, quality="low", max_usd=0.05)
    return image.to_dict()  # {"job_id": …, "model": …, "usd": "0.0060", "url": …, "files": […], "seconds": 14.2}


@app.post("/videos")
async def start_video(prompt: str):
    job = await client.submit("seedance-2-mini", prompt, duration=5, resolution="480p", max_usd=0.50)
    return {"job_id": job.id}  # video takes minutes: hand back the id, and let the page poll


@app.get("/videos/{job_id}")
async def video(job_id: str):
    status = await client.status(job_id)
    return (await client.result(job_id)).to_dict() if status.ok else {"state": status.state}
```

With Flask or Django, use the sync functions in the same way:

```python
@app.post("/images")
def make_image():
    return sume.run("gpt-image-2", request.json["prompt"], quality="low", max_usd=0.05).to_dict()
```

## Webhooks

Pass `webhook_url=` to `run` or `submit`, and Sume POSTs the finished job there when it ends. Each delivery is signed, and `verify_webhook` checks the signature:

```python
def handle(request):  # any framework: you need the raw body and the headers
    if not sume.verify_webhook(request.body, request.headers, signing_secret):
        return 401
    ...
```

The scheme is `sume-v1`: an HMAC-SHA256 over `{x-sume-webhook-timestamp}.{raw body}`, sent in `x-sume-webhook-signature`. Deliveries older than 10 minutes are refused (`tolerance=`). While a secret rotates, either the old or the new secret passes.

An open.sume.com key cannot read the signing secret through the API yet (`GET /v1/webhooks/signing-secret` answers `open_key_route_not_allowed`). Until it can, collect results with `job.wait()` or `sume.result(job_id)` instead.

## Errors

Every error is a `sume.SumeError`. `str(error)` says what went wrong, then what to do in a `Fix:` line. This one is real output, for an aspect ratio that gpt-image-2 does not take:

```text
sume.InvalidRequestError: Invalid request body, parameters, or headers. (request req_0f76b3e9…)
  - aspect_ratio: must be equal to one of the allowed values
Fix: gpt-image-2 takes aspect_ratio auto, 1:1, 16:9, 9:16, 4:3, 3:4, 5:4, 9:8, 4:5. See all its fields with sume.model("gpt-image-2").
```

When the API refuses fields, each one is in `error.field_errors`, to show next to a form:

```python
settings = {"aspect_ratio": "7:3"}  # say, from a form; as a keyword argument it would not type-check
try:
    sume.run("gpt-image-2", "a kite", quality="low", input=settings)
except sume.InvalidRequestError as error:
    for field in error.field_errors:
        print(field.path, field.message)  # aspect_ratio must be equal to one of the allowed values
```

| Error | When |
|---|---|
| `AuthenticationError` | There is no key, the key is wrong or revoked, or it was made for another host |
| `InsufficientFundsError` | The balance is below the price. Nothing was charged |
| `OverBudgetError` | The price is over `max_usd`. Nothing was submitted |
| `InvalidRequestError` | A field is missing, wrong, or not taken by this model (`field_errors` says which). `NotFoundError` covers an unknown model or job; for a model it suggests the closest id |
| `RateLimitError` | Too many requests, or a slot did not free up within the wait |
| `JobFailedError` | The job failed or was canceled. It is refunded; `job_id` and the reason are on the error |
| `WaitTimeoutError` | `timeout` passed. The job is still running; `sume.result(error.job_id)` picks it up |
| `APIError`, `APIConnectionError` | A problem on Sume's side, or the network. `retryable` says whether trying again can help |

Each error also has `code`, `hint`, `status`, `request_id`, `job_id` and `details`. Quote `request_id` to support. When a submit's outcome is unknown (the network dropped, or Sume answered 5xx), the error names the idempotency key in `idempotency_key` and in the `Fix:` line: pass it back to get that job instead of paying twice.

## Configuration

```python
client = sume.Client(
    api_key="sume_live_...",  # default: SUME_API_KEY, then ./.env
    base_url="https://api.open.sume.com",  # default: SUME_API_BASE_URL, then this
    timeout=60,  # seconds per HTTP request
    max_retries=3,  # for reads, and for submits (which carry an idempotency key)
    headers={"X-Trace-Id": "…"},  # sent to the API only, never to storage or media hosts
    http_client=None,  # your own httpx.Client, for proxies or tests
)
client.run("gpt-image-2", "a kite")
```

`sume.AsyncClient` takes the same arguments, with an `httpx.AsyncClient`. Close a client with `close()` (`await client.close()` for the async one), or use it as a context manager.

To see each API call, turn on debug logging for `sume`:

```python
import logging

logging.basicConfig()
logging.getLogger("sume").setLevel(logging.DEBUG)
# DEBUG:sume:POST /v1/image-router/generate -> 202 (request req_9c1…, attempt 1, 412 ms)
```

| Host | What it is |
|---|---|
| `https://api.open.sume.com` (default) | Open's production API. Keys come from open.sume.com |
| `https://open-api-development.up.railway.app` | Open's development API, for Sume staff. Keys come from open.dev.sume.com |
| `https://api.sume.com` | **Not Open.** This is sume.com's own API, and Open keys do not work there |

A key works only on the host it was made for. On another host it is refused as if it were wrong, and the error says so.

## Coming from fal or Higgsfield

| | fal-client | higgsfield-client | sume |
|---|---|---|---|
| Run and wait | `fal_client.subscribe("fal-ai/flux/dev", arguments={"prompt": p})` | `higgsfield_client.subscribe("bytedance/seedream/v4/text-to-image", arguments={"prompt": p})` | `sume.run("seedream-v4", p)` |
| Get the file | `result["images"][0]["url"]`, then download it yourself | `result["images"][0]["url"]`, then download it yourself | `result.save("out.png")` |
| Local file in | `fal_client.upload_file(path)`, then pass the URL | `higgsfield_client.upload_file(path)`, then pass the URL | `image="photo.png"` |
| Queue | `submit`, then `handle.status()`, `.get()`, `.cancel()` | `submit`, then `controller.status()`, `.get()`, `.cancel()` | `submit`, then `job.status()`, `.wait()`, `.cancel()` |
| Progress | `on_queue_update=`, `with_logs=True` | `on_queue_update=` | `on_status=`, `on_event=`, `on_submit=` |
| Price | Not in the client | Not in the client | `sume.estimate(…)` and `max_usd=` |
| Types | Arguments and results are dicts | Arguments and results are dicts | Typed model ids, fields and results |
| Errors | `FalClientHTTPError` | `HiggsfieldClientError` | One class per case, each with a `Fix:` line |

## Reference

Module functions use a shared default client; `sume.Client` and `sume.AsyncClient` have the same methods.

| Call | Returns |
|---|---|
| `run(model, prompt=None, /, *, max_usd, timeout, idempotency_key, on_submit, on_status, on_event, cancel_on_timeout, input, **inputs)` | `Result` |
| `submit(model, prompt=None, /, *, max_usd, idempotency_key, input, **inputs)` | `Job` |
| `estimate(model, prompt=None, /, *, input, **inputs)` | `Price` (`usd`, `usd_micros`, `str()` → `"$0.0060"`) |
| `models(kind=None)`, `model(id)` | `list[Model]`, `Model` (`id`, `name`, `kind`, `price`, `price_from`, `per`, `resolutions`, `seconds`, `inputs`) |
| `upload(file, *, content_type=None)` | the file's URL |
| `job(id)` | `Job`, without a call |
| `status(id)` | `Status` (`state`, `done`, `ok`, `queue_position`) |
| `result(id, *, timeout, on_status, on_event)` | `Result`, waiting if the job is still running |
| `cancel(id)` | `True` when canceled |
| `list_jobs(*, limit=20, status=None)` | `list[JobInfo]` |
| `verify_webhook(body, headers, secret, *, tolerance=600)` | `bool` |
| `job.status()`, `job.poll(*, timeout)`, `job.wait(*, timeout, on_status, on_event, cancel_on_timeout)`, `job.cancel()`, `job.events()`, `job.info()` | `Status`, an iterator of `Status`, `Result`, `bool`, `list[Event]`, `JobInfo` |
| `result.save(path=None)`, `file.save(path=None)`, `file.read()`, `result.to_dict()` | paths, a path, bytes, a dict |

## Also

- [`opensume`](https://github.com/sumelabs/opensume-cli), the command-line tool for the same API.
- [`@sume-com/open`](https://github.com/sumelabs/sume-js), the JavaScript and TypeScript client.
- API docs: https://open.sume.com/docs

## License

MIT
