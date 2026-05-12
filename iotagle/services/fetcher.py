"""Safe HTTP fetcher used by every service that touches the outside world.

Three layered defenses, in order:

1. SSRF guard runs on the initial URL **and** on every redirect target. The
   redirect loop is manual because ``requests``' built-in
   ``allow_redirects=True`` would skip the per-hop check.
2. A byte cap on the response body — enforced while streaming, so a malicious
   ``Content-Length`` header (or no header at all) can't bypass it.
3. Connect and read timeouts, totalling under gunicorn's worker timeout. A
   slow upstream can't tie up a worker indefinitely.

The UA is rotated per call from a small pool so a single fingerprint doesn't
turn into a rate-limit trigger.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urljoin

import requests
from requests import Response

from iotagle.config import config
from iotagle.security.ssrf import UnsafeURLError, validate_url


class FetchError(Exception):
    """Any failure during :func:`safe_get`.

    Carries an HTTP-shaped status hint so route handlers can decide on the
    user-facing response. ``status_hint`` is the suggested status code for
    the *iotagle* response, not the upstream's actual status.
    """

    def __init__(self, message: str, *, status_hint: int = 502):
        super().__init__(message)
        self.status_hint = status_hint


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a successful :func:`safe_get`."""

    content: bytes
    content_type: str
    final_url: str
    status_code: int


def _pick_user_agent() -> str:
    return random.choice(config.user_agents)  # noqa: S311 — not for crypto


def _read_capped(response: Response, max_bytes: int) -> bytes:
    """Stream the body and abort at ``max_bytes``.

    We ignore ``Content-Length`` for the cap check; a malicious server can
    set it to anything. The actual bytes are what matter.
    """
    buf = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > max_bytes:
            response.close()
            raise FetchError("response too large", status_hint=502)
    return bytes(buf)


def safe_get(
    url: str,
    *,
    max_bytes: int,
    accept: str = "*/*",
    extra_headers: Mapping[str, str] | None = None,
    user_agent: str | None = None,
) -> FetchResult:
    """Fetch ``url`` with every safety check applied.

    Raises :class:`FetchError` (with a status hint) on any problem. The
    SSRF guard runs against ``url`` and against every redirect target —
    a malicious server can't 302 us into the AWS metadata service.
    """
    headers = {
        "User-Agent": user_agent or _pick_user_agent(),
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    if extra_headers:
        headers.update(extra_headers)

    current_url = url
    timeout = (config.connect_timeout, config.read_timeout)

    # We follow redirects manually so the SSRF guard runs on every hop.
    for _hop in range(config.max_redirects + 1):
        try:
            validate_url(current_url)
        except UnsafeURLError as e:
            raise FetchError(f"blocked: {e}", status_hint=400) from e

        try:
            response = requests.get(
                current_url,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout as e:
            raise FetchError("upstream timeout", status_hint=504) from e
        except requests.RequestException as e:
            raise FetchError(f"upstream error: {type(e).__name__}", status_hint=502) from e

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise FetchError("redirect without Location header", status_hint=502)
            # Resolve relative redirects against the *current* URL, not the
            # original. Otherwise ``Location: /private`` from a benign-looking
            # host wouldn't get re-validated correctly.
            current_url = urljoin(current_url, location)
            continue

        # Real response — read body, respecting the byte cap.
        try:
            body = _read_capped(response, max_bytes=max_bytes)
        finally:
            response.close()

        if response.status_code >= 400:
            raise FetchError(
                f"upstream status {response.status_code}",
                status_hint=502 if response.status_code >= 500 else 404,
            )

        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        return FetchResult(
            content=body,
            content_type=content_type,
            final_url=response.url,
            status_code=response.status_code,
        )

    raise FetchError("too many redirects", status_hint=502)


__all__ = ("FetchError", "FetchResult", "safe_get")
