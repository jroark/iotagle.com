"""Transcoder tests.

We bypass the network by stubbing ``safe_get`` and ``_can_fetch`` to return
fixture content. The interesting properties are:

- All ``<a>`` and ``<img>`` are rewritten through ``/read`` / ``/image``.
- Tracking parameters are stripped.
- Scripts, styles, iframes, forms never survive.
- ``mailto:`` / ``javascript:`` / ``data:`` links are dropped.
- Same-page anchors (``#foo``) are preserved.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from bs4 import BeautifulSoup

from iotagle.services import transcoder
from iotagle.services.fetcher import FetchResult
from iotagle.services.transcoder import TranscodedPage, transcode


@pytest.fixture
def patch_safe_get(monkeypatch, fixture_text):
    def _patch(
        name: str = "sample_article.html",
        url: str = "https://example.com/article",
        content_type: str = "text/html; charset=utf-8",
    ):
        text = fixture_text(name)
        result = FetchResult(
            content=text.encode("utf-8"),
            content_type=content_type.split(";")[0].strip().lower(),
            final_url=url,
            status_code=200,
        )
        monkeypatch.setattr(transcoder, "safe_get", lambda *a, **k: result)
        monkeypatch.setattr(transcoder, "_can_fetch", lambda *_a, **_k: True)
        return result

    return _patch


def test_transcode_returns_html_body(patch_safe_get):
    patch_safe_get()
    page = transcode("https://example.com/article")
    assert isinstance(page, TranscodedPage)
    assert page.is_html
    assert page.title
    assert "<p>" in page.body_html.lower()


def test_no_scripts_or_styles_survive(patch_safe_get):
    patch_safe_get()
    page = transcode("https://example.com/article")
    body = page.body_html.lower()
    assert "<script" not in body
    assert "<style" not in body
    assert "<iframe" not in body
    assert "<form" not in body
    assert "onclick" not in body


def test_links_rewritten_through_read(patch_safe_get):
    patch_safe_get()
    page = transcode("https://example.com/article")
    soup = BeautifulSoup(page.body_html, "lxml")
    anchors = soup.find_all("a")
    assert anchors, "expected at least one anchor to survive cleanup"
    for a in anchors:
        href = a.get("href", "")
        # Either same-page anchor or /read?url=... — nothing else.
        assert href.startswith("#") or href.startswith(
            "/read?url="
        ), f"unexpected href survived: {href!r}"


def test_images_rewritten_through_image_proxy(patch_safe_get):
    patch_safe_get()
    page = transcode("https://example.com/article")
    soup = BeautifulSoup(page.body_html, "lxml")
    imgs = soup.find_all("img")
    # Readability may or may not preserve the image depending on its scoring,
    # but if any survive they must route through /image.
    for img in imgs:
        src = img.get("src", "")
        assert src.startswith("/image?url="), f"img src not rewritten: {src!r}"
        # srcset/data-* attributes must be gone.
        assert "srcset" not in img.attrs
        assert not any(k.startswith("data-") for k in img.attrs)


def test_tracking_params_stripped(patch_safe_get):
    patch_safe_get()
    page = transcode("https://example.com/article")
    soup = BeautifulSoup(page.body_html, "lxml")
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if not href.startswith("/read?url="):
            continue
        inner = parse_qs(urlparse(href).query)["url"][0]
        assert "utm_source" not in inner
        assert "fbclid" not in inner


def test_mailto_javascript_links_dropped(patch_safe_get):
    patch_safe_get()
    page = transcode("https://example.com/article")
    body = page.body_html.lower()
    assert "mailto:" not in body
    assert "javascript:" not in body


def test_same_page_anchor_preserved(patch_safe_get):
    patch_safe_get()
    page = transcode("https://example.com/article")
    soup = BeautifulSoup(page.body_html, "lxml")
    hrefs = [a.get("href", "") for a in soup.find_all("a")]
    # The fixture contains href="#section".
    assert any(h == "#section" for h in hrefs)


def test_non_html_content_returns_stub(patch_safe_get):
    patch_safe_get(content_type="application/pdf")
    page = transcode("https://example.com/whitepaper.pdf")
    assert page.is_html is False
    assert page.body_html == ""
    assert page.upstream_content_type == "application/pdf"


def test_blocked_by_remote_robots(monkeypatch):
    monkeypatch.setattr(transcoder, "_can_fetch", lambda *_a, **_k: False)
    from iotagle.services.transcoder import TranscodeError

    with pytest.raises(TranscodeError) as exc:
        transcode("https://example.com/")
    assert exc.value.status_hint == 403


def test_charset_detection_falls_back_to_utf8():
    """``_detect_charset`` is internal but worth pinning the priority order."""
    detect = transcoder._detect_charset
    assert detect(b"<html></html>", "text/html; charset=ISO-8859-1") == "iso-8859-1"
    assert (
        detect(b'<html><head><meta charset="windows-1252"></head></html>', "text/html")
        == "windows-1252"
    )
    assert detect(b"<html></html>", "text/html") == "utf-8"
