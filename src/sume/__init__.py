"""Images and video from the Sume Open API (https://open.sume.com).

    import sume

    image = sume.run("gpt-image-2", "a red kite over a beach", quality="low")
    image.save("kite.png")

The key comes from ``SUME_API_KEY`` (or that line in ``./.env``). The functions
here use a shared default client; make your own with ``sume.Client(api_key=...)``,
or ``sume.AsyncClient`` for asyncio (the module also has ``run_async`` and friends).
"""

from ._async_client import AsyncClient, AsyncFile, AsyncJob, AsyncResult
from ._client import Client, File, Job, Result
from ._constants import DEFAULT_BASE_URL
from ._errors import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    InsufficientFundsError,
    InvalidRequestError,
    JobFailedError,
    NotFoundError,
    OverBudgetError,
    RateLimitError,
    SumeError,
    WaitTimeoutError,
)
from ._types import Event, JobInfo, Model, Price, Status
from ._version import __version__
from ._webhooks import verify_webhook

# Shared default clients. Neither reads the key or the host until a call
# needs them, so setting SUME_API_KEY after `import sume` works.
_default = Client()
_default_async = AsyncClient()

run = _default.run
submit = _default.submit
job = _default.job
status = _default.status
result = _default.result
cancel = _default.cancel
estimate = _default.estimate
models = _default.models
model = _default.model
upload = _default.upload
list_jobs = _default.list_jobs

run_async = _default_async.run
submit_async = _default_async.submit
status_async = _default_async.status
result_async = _default_async.result
cancel_async = _default_async.cancel
estimate_async = _default_async.estimate
models_async = _default_async.models
model_async = _default_async.model
upload_async = _default_async.upload
list_jobs_async = _default_async.list_jobs

__all__ = [
    "DEFAULT_BASE_URL",
    "APIConnectionError",
    "APIError",
    "AsyncClient",
    "AsyncFile",
    "AsyncJob",
    "AsyncResult",
    "AuthenticationError",
    "Client",
    "Event",
    "File",
    "InsufficientFundsError",
    "InvalidRequestError",
    "Job",
    "JobFailedError",
    "JobInfo",
    "Model",
    "NotFoundError",
    "OverBudgetError",
    "Price",
    "RateLimitError",
    "Result",
    "Status",
    "SumeError",
    "WaitTimeoutError",
    "__version__",
    "cancel",
    "cancel_async",
    "estimate",
    "estimate_async",
    "job",
    "list_jobs",
    "list_jobs_async",
    "model",
    "model_async",
    "models",
    "models_async",
    "result",
    "result_async",
    "run",
    "run_async",
    "status",
    "status_async",
    "submit",
    "submit_async",
    "upload",
    "upload_async",
    "verify_webhook",
]
