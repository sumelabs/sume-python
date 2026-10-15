# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-10-15

Types for every model and field, progress callbacks, and a client that waits instead of failing when the account is busy.

### Added

- Types generated from the live API: `sume.ModelId` (64 models, split into `ImageModelId` and `VideoModelId`) and `sume.Inputs`, a `TypedDict` of every request field with the API's descriptions. `run`, `submit` and `estimate` take the fields as typed keyword arguments, so editors complete them and mypy or pyright catch a misspelled field or a wrong value. `input=` passes fields the types do not list yet. `scripts/generate.py` rewrites the types, and a weekly workflow fails when the API moves on.
- `on_submit=`, `on_status=` and `on_event=` on `run`, and `on_event=` on `wait` and `result`: the job's id as soon as it exists, each status read, and each step in the job's life, once.
- `cancel_on_timeout=True` on `run` and `wait` cancels the job (it is refunded) when the wait runs out before generation starts.
- When the account already runs as many jobs as it may, `run` and `submit` wait for a free slot and submit then, with the same idempotency key, instead of raising `RateLimitError`. `submit` waits up to 10 minutes; `run` waits within its `timeout`.
- `error.field_errors`: each field the API refused, as `FieldError(path, message)`. The `Fix:` line lists the values the model takes for those fields, from the catalog (`gpt-image-2 takes aspect_ratio auto, 1:1, 16:9, …`).
- Files are uploaded wherever they sit in the inputs, nested ones included (`frame_images`, `input_references`). A path string counts as a file where a media URL belongs, and nowhere else.
- `result.job` (input, timing, and `seconds`) and `result.to_dict()`, a JSON-ready summary for web handlers. `JobInfo.started_at` and `JobInfo.seconds`.
- `headers=` on `Client` and `AsyncClient`: extra headers for API calls, never sent to storage or media hosts.
- Debug logging on the `sume` logger: one line per API call with the status, request id, attempt and time.
- The README covers types, progress, cancel, nested uploads, FastAPI and Flask, logging, and a table for people coming from fal or Higgsfield. CI type-checks every example in it.

### Changed

- Models this version knows skip the catalog lookup before a submit. A model that is not in the catalog still gets a "Did you mean" suggestion.
- `Event.raw` is the event's JSON, as on every other type (`Event.data` still works).
- The package depends on `typing-extensions` as well as `httpx`.

## [0.1.0] - 2026-10-15

The first release: the whole generation lifecycle of the Sume Open API, sync and async.

### Added

- `sume.run(model, prompt, **inputs)`: prices the request, uploads any local files, submits with an idempotency key, waits, and returns a `Result`. `result.save()` downloads the files.
- `sume.submit`, `job.status`, `job.poll`, `job.wait`, `job.cancel`, `job.events` and `job.info`. `sume.result(job_id)`, `sume.status`, `sume.cancel` and `sume.list_jobs` work from an id alone.
- `sume.models`, `sume.model` and `sume.estimate`: the catalog and prices in dollars, free and with no key.
- `sume.upload` takes a path, bytes, an open binary file or a PIL image.
- `sume.AsyncClient`, and `run_async`, `submit_async` and the other module functions for asyncio.
- `sume.verify_webhook` checks `sume-v1` webhook signatures, and `webhook_url=` asks for a delivery.
- `max_usd` refuses an over-budget request before anything is uploaded or charged.
- Every error says what to do in a `Fix:` line and in `hint`.

[Unreleased]: https://github.com/sumelabs/sume-python/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/sumelabs/sume-python/releases/tag/v0.2.0
[0.1.0]: https://github.com/sumelabs/sume-python/releases/tag/v0.1.0
