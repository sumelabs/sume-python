"""Check that a webhook came from Sume."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Mapping


def verify_webhook(
    body: bytes | str,
    headers: Mapping[str, Any],
    secret: str,
    *,
    tolerance: float = 600,
    now: float | None = None,
) -> bool:
    """True when ``body`` was signed by Sume with ``secret`` within ``tolerance`` seconds.

    Pass the raw request body, exactly as received: re-serialized JSON will not
    match. ``secret`` is the signing secret of the account that made the job. The scheme is
    ``sume-v1``: HMAC-SHA256 over ``{x-sume-webhook-timestamp}.{body}``, sent
    as ``x-sume-webhook-signature: sume-v1=<hex>``. During a secret rotation the
    header carries two signatures, comma-separated; either one passes.
    ``tolerance=0`` turns off the replay check.
    """
    lower = {str(k).lower(): v for k, v in headers.items()}
    timestamp = str(lower.get("x-sume-webhook-timestamp") or "").strip()
    signatures = str(lower.get("x-sume-webhook-signature") or "")
    if not timestamp or not signatures or not secret:
        return False
    try:
        sent = int(timestamp)
    except ValueError:
        return False
    if tolerance and abs((time.time() if now is None else now) - sent) > tolerance:
        return False
    raw = body.encode("utf-8") if isinstance(body, str) else body
    expected = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + raw, hashlib.sha256).hexdigest()
    for part in signatures.split(","):
        scheme, _, digest = part.strip().partition("=")
        if scheme == "sume-v1" and hmac.compare_digest(digest.strip().lower(), expected):
            return True
    return False
