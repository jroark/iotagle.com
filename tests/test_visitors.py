"""Tests for the visitor log + UA classifier."""

from __future__ import annotations

import time

import pytest

from iotagle.services import visitors
from iotagle.services.ua_classify import is_vintage


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "visitors.db")
    visitors.reset_for_tests(p)
    yield p
    visitors.reset_for_tests(p)


def test_record_then_recent(db_path):
    visitors.record(db_path, "Mozilla/5.0 (...)", "/")
    out = visitors.recent(db_path)
    assert len(out) == 1
    assert out[0].ua == "Mozilla/5.0 (...)"
    assert out[0].hits == 1
    assert out[0].last_path == "/"
    assert out[0].first_seen == out[0].last_seen


def test_repeated_record_increments_hits(db_path):
    for path in ("/", "/about", "/search"):
        visitors.record(db_path, "Lynx/2.8.9rel.1", path)
        time.sleep(0.001)
    out = visitors.recent(db_path)
    assert len(out) == 1
    assert out[0].hits == 3
    assert out[0].last_path == "/search"  # most recent wins
    assert out[0].first_seen <= out[0].last_seen


def test_recent_orders_by_last_seen_desc(db_path):
    visitors.record(db_path, "first", "/")
    time.sleep(0.01)
    visitors.record(db_path, "second", "/")
    time.sleep(0.01)
    visitors.record(db_path, "third", "/")
    out = visitors.recent(db_path)
    assert [v.ua for v in out] == ["third", "second", "first"]


def test_recent_limit_clamps(db_path):
    for i in range(20):
        visitors.record(db_path, f"ua-{i}", f"/p{i}")
        time.sleep(0.001)
    out = visitors.recent(db_path, limit=5)
    assert len(out) == 5
    assert out[0].ua == "ua-19"
    assert out[-1].ua == "ua-15"


def test_empty_ua_skipped(db_path):
    visitors.record(db_path, "", "/")
    visitors.record(db_path, None, "/")
    visitors.record(db_path, "   ", "/")
    assert visitors.recent(db_path) == []


def test_long_ua_truncated(db_path):
    too_long = "x" * (visitors.MAX_UA_LENGTH + 50)
    visitors.record(db_path, too_long, "/")
    out = visitors.recent(db_path)
    assert len(out[0].ua) <= visitors.MAX_UA_LENGTH


def test_record_swallows_errors_when_db_dir_unwritable(tmp_path):
    """Bad path → record() must not raise. The page can be missing; requests can't fail."""
    # A read-only directory: chmod 0500 the parent, point at a file inside.
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        bad = str(ro / "blocked" / "visitors.db")  # can't even mkdir inside RO parent
        visitors.record(bad, "Lynx/2.8.9", "/")  # must not raise
    finally:
        ro.chmod(0o700)


def test_recent_empty_returns_empty_list(db_path):
    assert visitors.recent(db_path) == []
    assert visitors.recent(db_path, limit=0) == []
    assert visitors.recent(db_path, limit=-5) == []


# ---- is_vintage ------------------------------------------------------------


@pytest.mark.parametrize(
    "ua",
    [
        "Lynx/2.8.9rel.1 libwww-FM/2.14 SSL-MM/1.4.1 GNUTLS/3.6.13",
        "w3m/0.5.3+git20210102",
        "Dillo/3.0.5",
        "Mozilla/4.04 [en] (Macintosh; I; PPC)",
        "Mozilla/3.01Gold (Macintosh; I; 68K)",
        "Mozilla/1.22 (Windows; I; 16bit)",
        "Mozilla/4.5 [en] (X11; U; Linux 2.2.5 i686)",
        "Mosaic/2.7 (X11;SunOS 5.6 sun4u) libwww/2.12 modified",
        "NCSA Mosaic/3.0 (Windows 95)",
        "Mozilla/4.0 (compatible; MSIE 4.0; Windows 95)",
        "Mozilla/4.0 (compatible; MSIE 5.0; Mac_PowerPC)",
        "Opera/8.54 (Macintosh; PPC Mac OS X; U; en)",
        "iCab/2.9.8 (Macintosh; U; PPC; Mac OS 9)",
        "Mozilla/4.0 (compatible; MSIE 4.5; Mac_PowerPC)",
        "Arachne/1.91;beta;EN",
        "Mozilla/3.0 (compatible; NetPositive/2.1.1; BeOS)",
        # Windows 3.1 / 95 / 98 / NT 4 / CE
        "Mozilla/4.0 (compatible; MSIE 3.02; Windows 3.1)",
        "Mozilla/4.0 (compatible; MSIE 4.01; Windows 98)",
        "Mozilla/4.0 (compatible; MSIE 4.0; Windows NT 4)",
        "Mozilla/4.0 (compatible; MSIE 4.01; Windows CE; PPC)",
    ],
)
def test_is_vintage_true(ua):
    assert is_vintage(ua), f"expected vintage: {ua!r}"


@pytest.mark.parametrize(
    "ua",
    [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15",
        "curl/8.4.0",
        "Go-http-client/1.1",
        "python-requests/2.32.3",
        "",
        None,
    ],
)
def test_is_vintage_false(ua):
    assert not is_vintage(ua), f"expected NOT vintage: {ua!r}"
