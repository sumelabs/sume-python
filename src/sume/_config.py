"""Where the client finds its key and its host."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from ._constants import DEFAULT_BASE_URL, KEYS_URL
from ._errors import AuthenticationError, SumeError

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?SUME_API_KEY\s*=\s*(.*?)\s*$")


def normalize_base_url(raw: str) -> str:
    """``https://api.open.sume.com/v1/`` -> ``https://api.open.sume.com``.

    Plain http is accepted for a local server only: anywhere else it would
    send the key across the network in the clear.
    """
    text = raw.strip()
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        raise SumeError(
            f"base_url is not a URL: {raw!r}.",
            hint=f"Use the full address, like {DEFAULT_BASE_URL}.",
            code="invalid_base_url",
        )
    host = parts.hostname or ""
    if parts.scheme != "https" and not (parts.scheme == "http" and host in _LOCAL_HOSTS):
        raise SumeError(
            f"base_url must use https: {raw!r}.",
            hint="Plain http is accepted only for localhost.",
            code="invalid_base_url",
        )
    path = re.sub(r"/+$", "", parts.path)
    path = re.sub(r"/v1$", "", path)
    return f"{parts.scheme}://{parts.netloc}{path}"


def dotenv_key(directory: str | os.PathLike[str] | None = None) -> str | None:
    """``SUME_API_KEY`` from a ``.env`` file in ``directory`` (the working directory by default).

    Only that one line is read. The API host never comes from a project
    file, so a cloned repository cannot send your key elsewhere.
    """
    path = Path(directory or os.getcwd()) / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        match = _DOTENV_LINE.match(line)
        if not match:
            continue
        value = match.group(1)
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        return value.strip() or None
    return None


def resolve_api_key(explicit: str | None) -> str | None:
    """``api_key=`` first, then ``SUME_API_KEY``, then ``SUME_API_KEY`` in ``./.env``."""
    if explicit and explicit.strip():
        return check_key(explicit)
    from_env = os.environ.get("SUME_API_KEY")
    if from_env and from_env.strip():
        return check_key(from_env)
    from_file = dotenv_key()
    return check_key(from_file) if from_file else None


def check_key(raw: str) -> str:
    """A key goes into an HTTP header: refuse anything a key never contains, without quoting it."""
    key = raw.strip()
    if not re.fullmatch(r"[\x21-\x7e]+", key):
        raise AuthenticationError(
            "The API key contains spaces, line breaks or other characters a key never has.",
            hint=f"Copy the key again from {KEYS_URL}.",
            code="invalid_api_key",
        )
    return key


def resolve_base_url(explicit: str | None) -> str:
    """``base_url=`` first, then ``SUME_API_BASE_URL``, then Open's production API."""
    raw = explicit or os.environ.get("SUME_API_BASE_URL") or DEFAULT_BASE_URL
    return normalize_base_url(raw)
