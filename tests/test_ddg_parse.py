"""Tests for the DDG Lite parser, against a real captured fixture.

The fixture in ``tests/fixtures/ddg_results.html`` is a real POST response
from ``lite.duckduckgo.com/lite/`` for the query ``vintage macintosh``.
"""

from __future__ import annotations

import pytest

from iotagle.services import ddg
from iotagle.services.ddg import (
    DDGError,
    DDGUnavailable,
    Result,
    parse_results,
    reset_state_for_tests,
    search,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_state_for_tests()
    yield
    reset_state_for_tests()


def test_parse_returns_results(fixture_text):
    html = fixture_text("ddg_results.html")
    results = parse_results(html)
    assert len(results) >= 5
    assert all(isinstance(r, Result) for r in results)


def test_parse_titles_are_clean(fixture_text):
    html = fixture_text("ddg_results.html")
    results = parse_results(html)
    for r in results:
        assert r.title
        assert "\n" not in r.title  # snippet text shouldn't bleed in
        assert "  " not in r.title  # whitespace collapsed


def test_parse_urls_are_absolute_http(fixture_text):
    html = fixture_text("ddg_results.html")
    results = parse_results(html)
    for r in results:
        assert r.url.startswith(("http://", "https://"))
        assert "duckduckgo.com/l/" not in r.url  # uddg wrapping fully unwrapped


def test_parse_snippets_present_for_most_results(fixture_text):
    html = fixture_text("ddg_results.html")
    results = parse_results(html)
    with_snippet = [r for r in results if r.snippet]
    # DDG nearly always returns a snippet; allow one missing in case the
    # fixture happens to include an ad or special slot.
    assert len(with_snippet) >= len(results) - 1


def test_parse_respects_limit(fixture_text):
    html = fixture_text("ddg_results.html")
    results = parse_results(html, limit=3)
    assert len(results) == 3


def test_unwrap_uddg_in_synthetic_html():
    html = """
    <html><body><table>
      <tr><td><a class="result-link" rel="nofollow"
        href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fa%3D1">Example</a></td></tr>
      <tr><td class="result-snippet">An example.</td></tr>
    </table></body></html>
    """
    results = parse_results(html)
    assert len(results) == 1
    assert results[0].url == "https://example.com/page?a=1"
    assert results[0].snippet == "An example."


def test_parse_empty_html():
    assert parse_results("") == ()
    assert parse_results("<html><body>nothing here</body></html>") == ()


def test_parse_skips_non_http_schemes():
    html = """
    <html><body><table>
      <tr><td><a class="result-link" href="javascript:alert(1)">Bad</a></td></tr>
      <tr><td><a class="result-link" href="https://good.example/">Good</a></td></tr>
    </table></body></html>
    """
    results = parse_results(html)
    assert len(results) == 1
    assert results[0].title == "Good"


# --- search() integration ---------------------------------------------------


def test_search_returns_cached_result(monkeypatch, fixture_text):
    html = fixture_text("ddg_results.html")
    calls = {"n": 0}

    def fake_request(_query, **_kwargs):
        calls["n"] += 1
        return html

    monkeypatch.setattr(ddg, "_request", fake_request)

    a = search("vintage mac")
    b = search("Vintage Mac")  # case + whitespace normalization
    c = search(" vintage  mac ")
    assert a == b == c
    assert len(a) > 0
    assert calls["n"] == 1  # cached after first call


def test_search_empty_query_no_network(monkeypatch):
    monkeypatch.setattr(
        ddg,
        "_request",
        lambda *a, **k: pytest.fail("must not call network for empty query"),
    )
    assert search("") == ()
    assert search("   ") == ()


def test_search_retries_once_then_succeeds(monkeypatch, fixture_text):
    html = fixture_text("ddg_results.html")
    seq = iter([DDGError("first fails"), html])

    def fake_request(_query, **_kwargs):
        nxt = next(seq)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(ddg, "_request", fake_request)
    monkeypatch.setattr(ddg.time, "sleep", lambda _s: None)  # don't actually wait

    results = search("foo")
    assert len(results) > 0


def test_two_consecutive_failures_open_circuit(monkeypatch):
    def always_fails(_query, **_kwargs):
        raise DDGError("boom")

    monkeypatch.setattr(ddg, "_request", always_fails)
    monkeypatch.setattr(ddg.time, "sleep", lambda _s: None)

    with pytest.raises(DDGUnavailable):
        search("foo")
    # Circuit is now open; next call must short-circuit immediately.
    with pytest.raises(DDGUnavailable):
        search("bar")
