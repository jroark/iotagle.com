"""DuckDuckGo Lite scraper with TTL cache and a circuit breaker.

DDG Lite returns plain HTML that's almost transcoder-friendly already, so we
just extract titles, URLs, and snippets and hand them to the results template.

Resilience layers:

- **Cache**: a small TTLCache (10 min, 512 entries) absorbs duplicate queries
  that the same vintage workstation can fire when a user mashes Reload.
- **Circuit breaker**: two consecutive failures opens the circuit for 60 s,
  during which the search route renders ``ddg_unavailable.html`` instead of
  hammering DDG.

If DDG ever bans the AWS IP block outright, swap to a different backend by
implementing the same ``search(query) -> list[Result]`` shape elsewhere.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache

from iotagle.config import config


class DDGError(Exception):
    """Raised when DDG returns a non-OK response or the circuit is open."""


class DDGUnavailable(DDGError):
    """Circuit is open; don't try again until it closes."""


@dataclass(frozen=True)
class Result:
    title: str
    url: str
    snippet: str


# In-process LRU. Keyed by normalized query. Module-global is fine for a
# single gunicorn worker; cross-worker hits aren't worth a shared store at
# this scale.
_cache: TTLCache[str, tuple[Result, ...]] = TTLCache(
    maxsize=config.ddg_cache_max,
    ttl=config.ddg_cache_ttl_seconds,
)
_cache_lock = Lock()

# Circuit breaker state. Two strikes opens the circuit for ddg_circuit_open_seconds.
_breaker_failures = 0
_breaker_open_until = 0.0
_breaker_lock = Lock()


def _normalize(query: str) -> str:
    return " ".join(query.lower().split())


def _ua() -> str:
    # Round-robin per call via random pool; importing here avoids a circular
    # config dependency at module load.
    import random

    return random.choice(config.user_agents)  # noqa: S311


def _unwrap_uddg(href: str) -> str:
    """DDG sometimes wraps result links in ``/l/?uddg=<encoded>``; unwrap them.

    The Lite endpoint usually emits bare hrefs these days, but the wrapped
    form still appears for certain regions or for sponsored slots. Be liberal
    in what we accept.
    """
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path in ("/l/", "/l"):
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg")
        if uddg:
            return uddg[0]
    return href


def parse_results(html: str, *, limit: int = 20) -> tuple[Result, ...]:
    """Parse DDG Lite HTML into a tuple of :class:`Result`.

    The page uses a flat ``<table>`` of stacked ``<tr>``s — the row with
    ``a.result-link`` is one result, and the snippet sits on a following
    ``<tr>`` containing ``td.result-snippet``. We walk forward from each link
    rather than assuming a fixed offset, because DDG occasionally inserts an
    extra ``<tr>`` between them.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[Result] = []

    for anchor in soup.select("a.result-link"):
        title = anchor.get_text(" ", strip=True)
        href = (anchor.get("href") or "").strip()
        if not title or not href:
            continue
        href = _unwrap_uddg(href)
        if not href.startswith(("http://", "https://")):
            continue

        snippet = ""
        # Walk forward through sibling rows until we find a snippet or hit
        # the next result-link (in which case the snippet is missing).
        tr = anchor.find_parent("tr")
        cursor = tr
        for _ in range(4):
            if cursor is None:
                break
            cursor = cursor.find_next_sibling("tr")
            if cursor is None:
                break
            if cursor.find("a", class_="result-link"):
                # Hit the next result without finding a snippet.
                break
            td = cursor.find("td", class_="result-snippet")
            if td is not None:
                snippet = td.get_text(" ", strip=True)
                break

        results.append(Result(title=title, url=href, snippet=snippet))
        if len(results) >= limit:
            break

    return tuple(results)


def _trip_breaker() -> None:
    """Open the circuit immediately.

    Callers reach this only after a request *and* its retry have both failed
    — two upstream errors in quick succession is enough signal to back off
    for the configured cool-down. Earlier sketches used a counter, but the
    retry already absorbs one transient blip, so a single trip is the right
    granularity.
    """
    global _breaker_failures, _breaker_open_until  # noqa: PLW0603 — module-level state for a per-process circuit
    with _breaker_lock:
        _breaker_failures += 1
        _breaker_open_until = time.monotonic() + config.ddg_circuit_open_seconds


def _reset_breaker() -> None:
    global _breaker_failures, _breaker_open_until  # noqa: PLW0603 — module-level state for a per-process circuit
    with _breaker_lock:
        _breaker_failures = 0
        _breaker_open_until = 0.0


def _breaker_is_open() -> bool:
    with _breaker_lock:
        return time.monotonic() < _breaker_open_until


def _request(query: str, *, user_agent: str | None = None) -> str:
    """Issue one POST to DDG and return the body text.

    Raises :class:`DDGError` on 4xx/5xx or any transport error.
    """
    headers = {
        "User-Agent": user_agent or _ua(),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://lite.duckduckgo.com/",
        # ``Cache-Control: no-cache`` makes DDG less likely to serve a stale
        # rate-limit page from an edge.
        "Cache-Control": "no-cache",
    }
    data = {"q": query, "kl": config.ddg_region, "df": ""}
    try:
        response = requests.post(
            config.ddg_url,
            data=data,
            headers=headers,
            timeout=(config.connect_timeout, config.read_timeout),
            allow_redirects=False,
        )
    except requests.RequestException as e:
        raise DDGError(f"transport: {type(e).__name__}") from e

    if response.status_code != 200:
        raise DDGError(f"status {response.status_code}")
    return response.text


def search(query: str) -> tuple[Result, ...]:
    """Search DDG Lite for ``query`` and return parsed results.

    Empty queries return an empty tuple without hitting the network. Calls
    are deduplicated through an in-process TTL cache and gated by a circuit
    breaker that opens after two consecutive failures.

    Raises :class:`DDGUnavailable` when the circuit is open. Raises
    :class:`DDGError` for a single-call failure that doesn't (yet) trip the
    breaker — callers should catch both.
    """
    if not isinstance(query, str):
        return ()
    key = _normalize(query)
    if not key:
        return ()

    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    if _breaker_is_open():
        raise DDGUnavailable("circuit open")

    try:
        html = _request(query)
    except DDGError:
        # One retry, with a different UA, after a short backoff.
        time.sleep(1.5)
        try:
            html = _request(query)
        except DDGError as e:
            _trip_breaker()
            if _breaker_is_open():
                raise DDGUnavailable("circuit open after retries") from e
            raise

    results = parse_results(html)
    _reset_breaker()
    with _cache_lock:
        _cache[key] = results
    return results


def reset_state_for_tests() -> None:
    """Clear cache and breaker. Test fixtures call this between cases."""
    with _cache_lock:
        _cache.clear()
    _reset_breaker()


__all__ = (
    "DDGError",
    "DDGUnavailable",
    "Result",
    "parse_results",
    "reset_state_for_tests",
    "search",
)
