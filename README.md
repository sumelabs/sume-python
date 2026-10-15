# sume

The Python client for the [Sume Open API](https://open.sume.com). It can run any open.sume.com model for images, video, lip-sync and captions, priced in dollars. It has sync and async clients. Uploads, waiting, retries and downloads are handled for you.

```python
import sume

image = sume.run("gpt-image-2", "a red kite over a beach", quality="low")
image.save("kite.png")
```

That is the whole program. `run` prices the request, submits it, waits for the job, and returns the files. `save` writes them to disk and returns the paths.

- [Install](#install)
- [Key](#key)
- [Run a model](#run-a-model)
- [Pick a model](#pick-a-model)
- [Long jobs](#long-jobs)
- [Async](#async)
- [Files](#files)
- [Webhooks](#webhooks)
- [Errors](#errors)
- [Configuration](#configuration)

## Install

```sh
pip install https://github.com/sumelabs/sume-python/releases/latest/download/sume.tar.gz
```

Python 3.9 or newer. The only dependency is `httpx`. The package is not on PyPI yet; once it is, the install line will be `pip install sume`.

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
print(image.url, image.usd)  # https://media.sume.com/…/images.png 0.0077

# Edit an image: local files are uploaded first
night = sume.run("gpt-image-2", "the same cup at night, candlelight", image="cup.png")

# Image to video, with a budget
video = sume.run(
    "grok-imagine-video-1.5", "slow push in, steam rising", image="cup.png", duration=4, resolution="480p", max_usd=0.10
)
video.save("cup.mp4")

# Lip-sync a clip to speech
talk = sume.run("veed-lipsync", video="talk.mp4", audio="line.wav", duration=6)

# Burn captions into a clip
captioned = sume.run("veed-subtitles", video="clip.mp4", duration=12, captions={"style": "glass"})
```

- **Inputs** use the API's own field names: `quality`, `duration`, `resolution`, `aspect_ratio`, `num_images`, `output_format`, `captions`, and so on. `sume.model(id)` lists what a model takes.
- **Media** has short names: `image=`, `video=`, `audio=`, `end_image=`, `reference_images=` and `mask=`. Each value can be an https URL, a file path, bytes, an open binary file or a PIL image. Anything that is not a URL is uploaded first.
- **`max_usd`** refuses to submit a request that costs more. It raises `OverBudgetError` before anything is uploaded or charged.
- **`timeout`** is how long `run` waits, 30 minutes by default. When it passes, the job keeps running; see [Long jobs](#long-jobs).
- **Retries never pay twice.** Every submit carries an idempotency key, and the client's retries reuse it. To retry a submit yourself, pass `idempotency_key=`.

`run` returns a `Result`:

| | |
|---|---|
| `result.url`, `result.urls` | The file URLs |
| `result.files` | `File` objects with `url`, `type`, `content_type`, `width`, `height`, `duration_ms`, plus `save()` and `read()` |
| `result.save(path=None)` | Downloads every file and returns the paths. A file name: the first file takes it and the rest are numbered. A directory (ending in `/`): the files go inside it. No path: `<model>-<job>.<ext>` in the working directory, never overwriting a file |
| `result.usd` | The price in dollars, as a string like `"0.0077"` |
| `result.job_id`, `result.model` | Which job made it |

A video's poster frame is left out of `files`; `all_files` has every file the job made.

## Pick a model

Prices differ by more than 100× between models, so name the model yourself. Listing and pricing are free, and need no key:

```python
for m in sume.models("video"):  # image, video, lipsync or captions
    print(m.id, m.price, m.seconds)  # seedance-2-mini $0.0879–$0.1890/s (4, 15)

sume.model("seedance-2-mini")  # one model: inputs, resolutions, limits, price
sume.estimate("seedance-2-mini", "a paper boat", duration=8, resolution="720p")  # $1.5120
```

## Long jobs

Video can take minutes. Submit now and collect later:

```python
job = sume.submit("kling-3", "a red kite over a beach", duration=5)
print(job.id)  # keep this; it is all you need later

job.status()  # one read: queued, processing, completed, failed or canceled
for status in job.poll():  # every read until the job ends
    print(status)
video = job.wait()  # the Result, or JobFailedError

# Later, anywhere, with only the id:
video = sume.result("job_7513b52d-3669-4ffe-b317-8b02000715eb")
sume.cancel(job.id)  # before generation starts; refunded
job.events()  # job.created, job.started, job.completed…
sume.list_jobs(limit=10)  # newest first
```

`wait`, `poll` and `run` read the status as often as the API asks, at most every 5 s for the first minute and every 15 s after ten. Network errors and errors on Sume's side do not end the wait. When `timeout` passes, `WaitTimeoutError` names the job, and the job keeps running. Do not submit again: a second submit is a second charge.

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

```python
async with sume.AsyncClient() as client:
    jobs = [await client.submit("gpt-image-2", p, quality="low") for p in prompts]
    results = await asyncio.gather(*(job.wait() for job in jobs))
```

The module has `run_async`, `submit_async`, `status_async`, `result_async`, `cancel_async`, `estimate_async`, `models_async`, `model_async`, `upload_async` and `list_jobs_async`.

## Files

`run` uploads local files itself. To get a URL first, for reuse or for an inputs dict:

```python
url = sume.upload("photo.png")  # a path, bytes, an open binary file, or a PIL image
url = sume.upload(png_bytes, content_type="image/png")
```

Downloads stream to a `.part` file and are renamed when complete, so an interrupted download never leaves half a file behind. The API key is sent to the API only, never to the storage or media hosts.

## Webhooks

Pass `webhook_url=` to `run` or `submit`, and Sume POSTs the finished job there when it ends. Each delivery is signed, and `verify_webhook` checks the signature:

```python
def handle(request):  # any framework: you need the raw body and the headers
    if not sume.verify_webhook(request.body, request.headers, signing_secret):
        return 401
    ...
```

The scheme is `sume-v1`: an HMAC-SHA256 over `{x-sume-webhook-timestamp}.{raw body}`, sent in `x-sume-webhook-signature`. Deliveries older than 10 minutes are refused (`tolerance=`). While a secret rotates, either the old or the new secret passes.

An open.sume.com key cannot read the signing secret through the API (`GET /v1/webhooks/signing-secret` answers `open_key_route_not_allowed`). Until it can, you can poll with `job.wait()` or `sume.result(job_id)` instead of relying on webhooks.

## Errors

Every error is a `sume.SumeError`. `str(error)` gives what went wrong, then a `Fix:` line:

```text
sume.InsufficientFundsError: Balance too low. (request req_4f2…)
Fix: Top up at https://open.sume.com/billing. It needs $0.3516; the balance is $0.12.
```

| Error | When |
|---|---|
| `AuthenticationError` | There is no key, the key is wrong or revoked, or it was made for another host |
| `InsufficientFundsError` | The balance is below the price. Nothing was charged |
| `OverBudgetError` | The price is over `max_usd`. Nothing was submitted |
| `InvalidRequestError` | A field is missing, wrong, or not taken by this model. `NotFoundError` covers an unknown model or job; for a model it suggests the closest id |
| `RateLimitError` | Too many requests, or too many jobs running at once |
| `JobFailedError` | The job failed or was canceled. It is refunded; `job_id` and the reason are on the error |
| `WaitTimeoutError` | `timeout` passed. The job is still running; `sume.result(error.job_id)` picks it up |
| `APIError`, `APIConnectionError` | A problem on Sume's side, or the network. `retryable` says whether trying again can help |

Each error also has `code`, `hint`, `status`, `request_id`, `job_id` and `details`. Quote `request_id` to support.

## Configuration

```python
client = sume.Client(
    api_key="sume_live_...",  # default: SUME_API_KEY, then ./.env
    base_url="https://api.open.sume.com",  # default: SUME_API_BASE_URL, then this
    timeout=60,  # seconds per HTTP request
    max_retries=3,  # for reads, and for submits (which carry an idempotency key)
    http_client=None,  # your own httpx.Client, for proxies or tests
)
client.run("gpt-image-2", "a kite")
```

| Host | What it is |
|---|---|
| `https://api.open.sume.com` (default) | Open's production API. Keys come from open.sume.com |
| `https://open-api-development.up.railway.app` | Open's development API, for Sume staff. Keys come from open.dev.sume.com |
| `https://api.sume.com` | **Not Open.** This is sume.com's own API, and Open keys do not work there |

A key works only on the host it was made for. On another host it is refused as if it were wrong, and the error says so.

## Also

- [`opensume`](https://github.com/sumelabs/opensume-cli), the command-line tool for the same API.
- [`@sume-com/open`](https://github.com/sumelabs/sume-js), the JavaScript and TypeScript client.
- API docs: https://open.sume.com/docs

## License

MIT
