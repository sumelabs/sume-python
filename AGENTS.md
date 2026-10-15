# AGENTS.md

## Using `sume` in your code

```python
import sume  # pip install https://github.com/sumelabs/sume-python/releases/latest/download/sume.tar.gz

image = sume.run("gpt-image-2", "a red kite over a beach", quality="low")  # waits, returns a Result
paths = image.save("out/")  # downloads; returns the paths
```

- Key: `SUME_API_KEY` in the environment or `./.env`. A person makes it at https://open.sume.com/api-keys. Never print it.
- Host: `https://api.open.sume.com` by default. Never `https://api.sume.com`, which is sume.com's API.
- Name the model yourself; prices differ by more than 100×. `sume.models("video")` lists models with `m.price` (per image or per second). `sume.estimate(...)` is free.
- Pass `max_usd=` to cap a run. Local files work wherever a URL does (`image="photo.png"`).
- Long jobs: `job = sume.submit(...)`, then later `sume.result(job.id)`. After `WaitTimeoutError` the job keeps running. Never resubmit; a second submit is a second charge.
- Progress: `on_submit=`, `on_status=`, `on_event=`. To give up on a queued job and get refunded: `cancel_on_timeout=True`.
- Model ids and fields are typed (`sume.ModelId`, `sume.Inputs`), so mypy or pyright catch a misspelled field. A field the types lack goes through `input={...}`.
- Errors are `sume.SumeError` subclasses. `str(error)` ends with a `Fix:` line; `error.field_errors` lists refused fields.

## Working on this repository

```sh
python3 -m venv .venv && .venv/bin/pip install -e . pytest anyio ruff mypy build
.venv/bin/python -m pytest          # against a mock API (tests/conftest.py); never the real one
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
.venv/bin/mypy --strict tests/typing_check.py   # the mistakes the types must catch
.venv/bin/python scripts/check_readme.py         # every README example type-checks
```

- `src/sume/_client.py` and `_async_client.py` hold the same methods, sync and async; change both. Logic that does no I/O lives in `_flow.py`, `_inputs.py`, `_files.py` and `_types.py`.
- `src/sume/_generated.py` comes from the live API: run `python scripts/generate.py`, never edit it by hand. The weekly `types.yml` workflow fails when it falls behind.
- `_http.py` owns the key, the `data`/`error` envelope, request ids and retries. It retries only what is safe: reads, and submits that carry an idempotency key. The key goes to the API host only, never to storage or media URLs.
- Every error says what to do (`hint`). A new error path needs a hint, and a test that checks it.
- A user-visible change gets a CHANGELOG line under **Unreleased** and README updates. Bump the version in `pyproject.toml` and `src/sume/_version.py`, then push a `v*` tag; the release workflow builds, checks and publishes.
