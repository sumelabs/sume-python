"""The parts of a run that do not depend on sync or async I/O."""

from __future__ import annotations

import difflib
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from ._errors import JobFailedError, NotFoundError, OverBudgetError, SumeError, WaitTimeoutError
from ._inputs import describe, media_slots, with_media
from ._types import Price, Status

DEFAULT_WAIT = 1800.0
"""How long ``run`` and ``wait`` wait by default: 30 minutes. Video can take 10 or more."""

MIN_DELAY = 1.0
DEFAULT_DELAY = 2.0


def max_delay(elapsed: float) -> float:
    """The longest gap between status reads, by how long the wait has run.

    The API's ``next_poll_after_seconds`` can ask for 30 s while a job runs,
    but an image often finishes in 15; following it blindly doubles the wait.
    Shorter hints are always honored.
    """
    if elapsed < 60:
        return 5.0
    if elapsed < 600:
        return 10.0
    return 15.0


def next_delay(status: Status, headers: Mapping[str, str], elapsed: float) -> float:
    hint = status.next_poll_after_seconds
    if hint is None:
        try:
            hint = float(headers.get("x-sume-poll-after") or 0) or None
        except ValueError:
            hint = None
    if hint is None:
        hint = status.raw.get("recommended_poll_interval_seconds")
    seconds = float(hint) if isinstance(hint, (int, float)) and hint > 0 else DEFAULT_DELAY
    return min(max(seconds, MIN_DELAY), max_delay(elapsed))


def first_delay(submit: Mapping[str, Any]) -> float:
    if submit.get("terminal"):
        return 0.0
    hint = submit.get("next_poll_after_seconds")
    return min(max(float(hint), 0.0), max_delay(0)) if isinstance(hint, (int, float)) else 0.0


def timeout_error(job_id: str, timeout: float) -> WaitTimeoutError:
    return WaitTimeoutError(
        f"Stopped waiting after {round(timeout)} s; job {job_id} is still running.",
        hint=f'Pick it up later with sume.result("{job_id}"). Do not submit again: a second submit is a second charge.',
        code="wait_timeout",
        job_id=job_id,
    )


def failure(job_id: str, state: str, job: Mapping[str, Any] | None) -> JobFailedError:
    error = (job or {}).get("error") or {}
    reason = (
        error.get("message") or error.get("public_reason") or error.get("code") if isinstance(error, Mapping) else None
    )
    if state == "canceled":
        return JobFailedError(
            f"Job {job_id} was canceled.",
            hint="Canceled jobs are refunded.",
            code="job_canceled",
            job_id=job_id,
        )
    return JobFailedError(
        f"Job {job_id} failed: {reason or 'the provider gave no reason'}.",
        hint="Failed jobs are refunded. Change the input, or try another model.",
        code=(error.get("code") if isinstance(error, Mapping) else None) or "job_failed",
        job_id=job_id,
        retryable=bool(isinstance(error, Mapping) and error.get("retryable")),
    )


def check_budget(price: Price, max_usd: float | Decimal | str) -> None:
    if Decimal(price.usd) > Decimal(str(max_usd)):
        raise OverBudgetError(
            f"This costs ${price.usd}, over max_usd=${max_usd}. Nothing was submitted.",
            hint="Raise max_usd, or lower the settings (duration, resolution, quality).",
            code="over_budget",
        )


def priced(get: Callable[[], Price]) -> Price:
    """The price, or why max_usd could not be checked (and so nothing was submitted)."""
    try:
        return get()
    except SumeError as error:
        raise SumeError(
            f"Could not price the request ({error.message}), so max_usd cannot be checked. Nothing was submitted.",
            hint=error.hint,
            code=error.code,
            status=error.status,
            request_id=error.request_id,
            retryable=error.retryable,
        ) from None


def model_not_found(model_id: str, known: Sequence[str]) -> NotFoundError:
    guess = difflib.get_close_matches(model_id, known, n=1, cutoff=0.6)
    return NotFoundError(
        f'No model "{model_id}" in the open catalog.',
        hint=f"Did you mean {guess[0]}?" if guess else "List the models with sume.models().",
        code="model_not_found",
        status=404,
    )


def estimate_input(body: Mapping[str, Any]) -> dict[str, Any]:
    """The body with placeholders for files not yet uploaded: the price does not depend on their URLs."""
    slots = list(media_slots(body))
    return with_media(body, [f"https://upload.invalid/{describe(value) or 'file'}" for _, _, value in slots])
