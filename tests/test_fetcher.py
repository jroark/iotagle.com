"""Fetcher tests: byte cap, overall deadline, and per-hop SSRF checks."""

from __future__ import annotations

import pytest
import responses

from iotagle.config import config
from iotagle.services import fetcher
from iotagle.services.fetcher import FetchError, _read_capped, safe_get


class _FakeResponse:
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


class _Clock:
    """Deterministic stand-in for time.monotonic that advances per call."""

    def __init__(self, step: float):
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_read_capped_returns_body():
    resp = _FakeResponse([b"abc", b"", b"def"])
    assert _read_capped(resp, max_bytes=10, deadline=float("inf")) == b"abcdef"


def test_read_capped_enforces_byte_cap():
    resp = _FakeResponse([b"x" * 6, b"x" * 6])
    with pytest.raises(FetchError, match="too large"):
        _read_capped(resp, max_bytes=10, deadline=float("inf"))
    assert resp.closed


def test_read_capped_enforces_deadline(monkeypatch):
    """A slow-drip upstream is cut off at the deadline, not at gunicorn's kill."""
    monkeypatch.setattr(fetcher.time, "monotonic", _Clock(step=1.0))
    resp = _FakeResponse([b"x"] * 100)
    with pytest.raises(FetchError, match="timeout") as exc:
        _read_capped(resp, max_bytes=1_000_000, deadline=5.0)
    assert exc.value.status_hint == 504
    assert resp.closed


@responses.activate
def test_safe_get_deadline_spans_whole_fetch(monkeypatch):
    monkeypatch.setattr(fetcher, "validate_url", lambda _u: None)
    # Each monotonic() call advances past the whole budget.
    monkeypatch.setattr(fetcher.time, "monotonic", _Clock(step=config.total_timeout + 1))
    responses.get("http://example.com/", body=b"hello", content_type="text/html")
    with pytest.raises(FetchError, match="timeout"):
        safe_get("http://example.com/", max_bytes=1000)


@responses.activate
def test_safe_get_happy_path(monkeypatch):
    monkeypatch.setattr(fetcher, "validate_url", lambda _u: None)
    responses.get("http://example.com/", body=b"hello", content_type="text/html; charset=utf-8")
    result = safe_get("http://example.com/", max_bytes=1000)
    assert result.content == b"hello"
    assert result.content_type == "text/html"


@responses.activate
def test_safe_get_revalidates_redirect_target(monkeypatch):
    seen: list[str] = []

    def _validate(url):
        seen.append(url)
        if "169.254" in url:
            raise fetcher.UnsafeURLError("address in blocked range")

    monkeypatch.setattr(fetcher, "validate_url", _validate)
    responses.get(
        "http://example.com/",
        status=302,
        headers={"Location": "http://169.254.169.254/latest/meta-data/"},
    )
    with pytest.raises(FetchError, match="blocked") as exc:
        safe_get("http://example.com/", max_bytes=1000)
    assert exc.value.status_hint == 400
    assert seen == ["http://example.com/", "http://169.254.169.254/latest/meta-data/"]
