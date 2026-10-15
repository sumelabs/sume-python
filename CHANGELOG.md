# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/sumelabs/sume-python/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sumelabs/sume-python/releases/tag/v0.1.0
