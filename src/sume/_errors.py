"""Every error the client raises. Each one says what went wrong and, in ``hint``, what to do."""

from __future__ import annotations

from typing import Any, Mapping

from ._constants import BILLING_URL, DEFAULT_BASE_URL, KEYS_URL, WRONG_HOST
from ._types import FieldError


class SumeError(Exception):
    """Base class. ``str(error)`` is the message, then the fix on its own line.

    Attributes:
        message: What went wrong.
        hint: What to do about it, when there is something to do.
        code: A stable machine-readable code, such as ``insufficient_funds``.
        status: The HTTP status, for errors the API returned.
        request_id: The API's request id; quote it to support.
        retryable: Whether the same call may succeed if you try again.
        details: The API's structured details, when it sent any.
        job_id: The job this error is about, when there is one.
        idempotency_key: The key a failed submit carried. Pass it back as
            ``idempotency_key=`` to get that job instead of paying again.
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        code: str = "error",
        status: int | None = None,
        request_id: str | None = None,
        retryable: bool = False,
        details: Any = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        retry_after: float | None = None,
        field_errors: list[FieldError] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code
        self.status = status
        self.request_id = request_id
        self.retryable = retryable
        self.details = details
        self.job_id = job_id
        self.idempotency_key = idempotency_key
        self.retry_after = retry_after
        """Seconds the API asked to wait before retrying, when it said."""
        self.field_errors: list[FieldError] = field_errors or []
        """The fields the API refused, one by one, for showing next to a form."""

    def __str__(self) -> str:
        line = self.message
        if self.request_id:
            line += f" (request {self.request_id})"
        for field in self.field_errors:
            line += f"\n  - {field}"
        if self.hint:
            line += f"\nFix: {self.hint}"
        return line


class AuthenticationError(SumeError):
    """No key, a wrong or revoked key, or a call the key may not make."""


class InsufficientFundsError(SumeError):
    """The open.sume.com balance is lower than the price. Nothing was charged."""


class OverBudgetError(SumeError):
    """The price is over ``max_usd``. Nothing was submitted."""


class InvalidRequestError(SumeError):
    """The API refused the request: a field is missing, wrong, or not taken by this model."""


class NotFoundError(InvalidRequestError):
    """No such model or job."""


class RateLimitError(SumeError):
    """Too many requests, or as many jobs in flight as the account may run."""


class JobFailedError(SumeError):
    """The job failed or was canceled. Failed jobs are refunded."""


class WaitTimeoutError(SumeError):
    """Stopped waiting. The job is still running on Sume; pick it up with ``result(job_id)``."""


class APIError(SumeError):
    """Something went wrong on Sume's side."""


class APIConnectionError(SumeError):
    """The API could not be reached."""


def _other_host_hint(base_url: str) -> str:
    if base_url.split("://", 1)[-1].split("/", 1)[0] == WRONG_HOST:
        return f"{WRONG_HOST} is sume.com's API, not Open's. Use {DEFAULT_BASE_URL} (the default)."
    if base_url != DEFAULT_BASE_URL:
        return (
            f"This client calls {base_url}. A key works only on the API it was made for; "
            f"keys from {KEYS_URL} are for {DEFAULT_BASE_URL}."
        )
    return f"The key is wrong or revoked. Make a new one at {KEYS_URL}."


def field_errors(details: Any) -> list[FieldError]:
    """``details.errors[]`` as ``FieldError``s."""
    errors = details.get("errors") if isinstance(details, Mapping) else None
    if not isinstance(errors, list):
        return []
    out = []
    for entry in errors:
        if isinstance(entry, Mapping) and isinstance(entry.get("message"), str):
            path = entry.get("path")
            out.append(FieldError(path=path if isinstance(path, str) else "", message=entry["message"]))
    return out


def error_from_response(
    status: int,
    body: Mapping[str, Any],
    *,
    request_id: str | None,
    base_url: str,
) -> SumeError:
    """Turn the API's ``error`` envelope into the error class that says what to do."""
    code = str(body.get("code") or f"http_{status}")
    message = str(body.get("message") or f"The API answered {status}.")
    details = body.get("details")
    retryable = bool(body.get("retryable", status == 429 or status >= 500))
    request_id = body.get("request_id") or request_id
    wait = body.get("retry_after_seconds")
    common: dict[str, Any] = {
        "code": code,
        "status": status,
        "request_id": request_id,
        "retryable": retryable,
        "details": details,
        "retry_after": float(wait) if isinstance(wait, (int, float)) and wait > 0 else None,
    }
    fields = field_errors(details)
    if fields:
        common["field_errors"] = fields

    # A provider can refuse a job the moment it is submitted: the API answers
    # with the job already failed. That is a failed job (refunded), not a bad request.
    job = details.get("job") if isinstance(details, Mapping) else None
    if isinstance(job, Mapping) and job.get("status") in ("failed", "canceled") and isinstance(job.get("id"), str):
        reason = (job.get("error") or {}).get("message") if isinstance(job.get("error"), Mapping) else None
        return JobFailedError(
            f"Job {job['id']} {job['status']}: {reason or message}",
            hint="Failed jobs are refunded. Change the input, or try another model.",
            job_id=job["id"],
            **{k: v for k, v in common.items() if k != "retryable"},
            retryable=False,
        )

    if code == "unauthorized" or status == 401:
        return AuthenticationError(message, hint=_other_host_hint(base_url), **common)
    if code == "open_key_route_not_allowed":
        return AuthenticationError(
            message, hint="open.sume.com keys call models, prices, images, video and jobs only.", **common
        )
    if code in ("api_key_not_accepted", "session_not_accepted"):
        public = details.get("public_api_base_url") if isinstance(details, Mapping) else None
        hint = f"Send keys to the public API: base_url={public!r}." if isinstance(public, str) else None
        return AuthenticationError(message, hint=hint or _other_host_hint(base_url), **common)
    if status == 403:
        return AuthenticationError(message, hint=_other_host_hint(base_url), **common)
    if code == "insufficient_funds" or status == 402:
        need = details.get("required_usd") if isinstance(details, Mapping) else None
        have = details.get("available_usd") if isinstance(details, Mapping) else None
        amounts = (
            f" It needs ${need}; the balance is ${have}." if isinstance(need, str) and isinstance(have, str) else ""
        )
        return InsufficientFundsError(message, hint=f"Top up at {BILLING_URL}.{amounts}", **common)
    if code in ("concurrency_limit_reached", "queue_full"):
        return RateLimitError(
            message,
            hint="The account has as many jobs in flight as it may. Wait for one to finish, then submit again.",
            **common,
        )
    if status == 429:
        return RateLimitError(message, hint="Slow down, then retry after a moment.", **common)
    if code == "model_not_found":
        return NotFoundError(message, hint="List the models with sume.models().", **common)
    if code in ("job_not_found", "not_found") or status == 404:
        return NotFoundError(message, hint="Check the id; jobs belong to the account whose key made them.", **common)
    if code in ("job_not_cancelable", "job_generation_already_started"):
        return InvalidRequestError(
            message,
            hint="Generation has started; the job will finish or fail on its own. Failed jobs are refunded.",
            **common,
        )
    if code == "job_not_completed":
        return InvalidRequestError(message, hint="The job has not finished. Wait for it with result(job_id).", **common)
    if code == "idempotency_conflict":
        return InvalidRequestError(
            message, hint="That idempotency key belongs to a different request. Use a new key.", **common
        )
    if 400 <= status < 500:
        return InvalidRequestError(message, hint="Check the model's fields and limits with sume.model(id).", **common)
    hint = (
        "This is on Sume's side, and the same request will fail again. Quote the request id to support."
        if body.get("retryable") is False
        else "This is on Sume's side. Retry; quote the request id if it persists."
    )
    return APIError(message, hint=hint, **common)
