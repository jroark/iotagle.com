"""Reader-mode page transcoder.

Given an upstream URL, fetch the page, run it through Readability to isolate
the article body, then strip everything a 1995 browser doesn't understand:

- All scripts, styles, iframes, forms, media tags, SVG, canvas.
- All attributes except a short per-tag allowlist (``href``, ``src``,
  ``alt``, ``width``, ``height``, ``colspan``, ``rowspan``).
- All outbound URLs are rewritten back through ``/read`` (for anchors) or
  ``/image`` (for images), so following a link keeps the user inside the
  transcoder.

We honor the remote site's ``robots.txt``. If they say no, we say no.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag
from readability import Document  # provided by readability-lxml

from iotagle.config import config
from iotagle.security.ssrf import UnsafeURLError, validate_url
from iotagle.services.fetcher import FetchError, safe_get

# Tags we always remove entirely, content and all. ``<noscript>`` goes too —
# it sometimes contains escaped HTML that BS4 would otherwise leave inline.
_STRIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "iframe",
        "frame",
        "frameset",
        "form",
        "input",
        "button",
        "select",
        "option",
        "textarea",
        "label",
        "svg",
        "canvas",
        "video",
        "audio",
        "source",
        "track",
        "object",
        "embed",
        "param",
        "applet",
        "link",
        "meta",
        "base",
        "template",
        "slot",
    }
)

# Per-tag attribute allowlist. Everything else gets dropped.
_ATTR_ALLOW: dict[str, frozenset[str]] = {
    "a": frozenset({"href"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan"}),
    "table": frozenset({"border"}),
}

# Tracking parameters we strip from rewritten links. Not exhaustive, but
# covers what most pages actually emit.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "ref_src",
        "ref_url",
    }
)


class TranscodeError(Exception):
    """Raised when transcoding fails before we can produce body HTML."""

    def __init__(self, message: str, *, status_hint: int = 502):
        super().__init__(message)
        self.status_hint = status_hint


@dataclass(frozen=True)
class TranscodedPage:
    title: str
    body_html: str
    original_url: str
    is_html: bool = True
    upstream_content_type: str = "text/html"
    upstream_size: int = 0


def _detect_charset(raw: bytes, content_type: str) -> str:
    """Best-effort charset detection.

    Order: HTTP ``Content-Type: charset=…``, then ``<meta charset>``, then
    UTF-8. Always errors='replace' on the eventual decode — losing a glyph
    is better than crashing the route.
    """
    # 1. HTTP header
    for raw_part in content_type.split(";"):
        part = raw_part.strip().lower()
        if part.startswith("charset="):
            return part[len("charset=") :].strip().strip('"').strip("'") or "utf-8"
    # 2. <meta charset>. Sniff in the first 4 KB.
    head = raw[:4096].lower()
    idx = head.find(b"charset=")
    if idx >= 0:
        tail = head[idx + len("charset=") :]
        # Strip optional leading quote; some pages use ``charset="utf-8"``
        # and others use the bare form ``charset=utf-8``.
        if tail[:1] in (b'"', b"'"):
            tail = tail[1:]
        end = len(tail)
        for sep in (b'"', b"'", b";", b" ", b">", b"/"):
            i = tail.find(sep)
            if 0 <= i < end:
                end = i
        candidate = tail[:end].decode("ascii", errors="ignore").strip()
        if candidate:
            return candidate
    return "utf-8"


def _decode(raw: bytes, content_type: str) -> str:
    charset = _detect_charset(raw, content_type)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        # Unknown charset name — fall back to UTF-8.
        return raw.decode("utf-8", errors="replace")


def _strip_tracking_params(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    filtered = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    return urlunparse(parsed._replace(query=urlencode(filtered)))


def _rewrite_link(base: str, href: str) -> str | None:
    """Return a rewritten ``href`` pointing through ``/read``, or None to drop.

    Same-page anchors (``#section``) are preserved untouched. Anything that
    isn't ``http(s)`` after resolving relative URLs gets dropped — that
    includes ``mailto:``, ``tel:``, ``javascript:``, ``data:``.
    """
    href = href.strip()
    if not href:
        return None
    # Pure fragment — keep as-is.
    if href.startswith("#"):
        return href
    absolute = urljoin(base, href)
    scheme = urlparse(absolute).scheme.lower()
    if scheme not in ("http", "https"):
        return None
    absolute = _strip_tracking_params(absolute)
    return "/read?" + urlencode({"url": absolute})


def _rewrite_img(base: str, src: str) -> str | None:
    src = src.strip()
    if not src or src.startswith("data:"):
        return None
    absolute = urljoin(base, src)
    scheme = urlparse(absolute).scheme.lower()
    if scheme not in ("http", "https"):
        return None
    return "/image?" + urlencode({"url": absolute})


def _scrub_attributes(tag: Tag) -> None:
    allow = _ATTR_ALLOW.get(tag.name, frozenset())
    for attr in list(tag.attrs):
        if attr not in allow:
            del tag.attrs[attr]


def _clean(soup: BeautifulSoup, base_url: str) -> None:
    """Mutate ``soup`` in place: drop disallowed tags, scrub attributes, rewrite URLs."""
    # 1. Strip dangerous tags entirely.
    for tag_name in _STRIP_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()

    # 2. Walk every tag once for attribute scrubbing + URL rewriting.
    for el in list(soup.find_all(True)):
        # Some elements may have been removed already as a descendant of a
        # stripped parent — skip if detached.
        if el.parent is None and el is not soup:
            continue

        if el.name == "a":
            href = el.get("href", "")
            new = _rewrite_link(base_url, href) if href else None
            _scrub_attributes(el)
            if new is None:
                # The link can't safely point anywhere — unwrap the anchor so
                # its text remains in the document but it stops being a link.
                # ``unwrap`` replaces the tag with its children.
                el.unwrap()
                continue
            el["href"] = new
        elif el.name == "img":
            src = el.get("src", "")
            new = _rewrite_img(base_url, src) if src else None
            # Preserve alt/width/height before scrubbing.
            alt = el.get("alt")
            width = el.get("width")
            height = el.get("height")
            _scrub_attributes(el)
            if new is None:
                el.decompose()
                continue
            el["src"] = new
            if alt is not None:
                el["alt"] = alt
            if width is not None:
                el["width"] = width
            if height is not None:
                el["height"] = height
        else:
            _scrub_attributes(el)


def _can_fetch(url: str) -> bool:
    """Check the remote site's robots.txt before we fetch.

    The fetch goes through :func:`safe_get` so it inherits our timeouts and
    SSRF guard. A failure to read robots.txt is treated as "allowed" — most
    small sites don't ship one, and refusing on read failure would block far
    too much legitimate use.
    """
    parsed = urlparse(url)
    if not parsed.netloc:
        return False
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        # 64 KB is plenty for any sane robots.txt.
        result = safe_get(
            robots_url,
            max_bytes=64 * 1024,
            accept="text/plain,*/*;q=0.8",
            user_agent=config.reader_user_agent,
        )
    except FetchError:
        return True

    try:
        body = result.content.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return True

    rp = robotparser.RobotFileParser()
    rp.parse(body.splitlines())
    return rp.can_fetch(config.reader_user_agent, url)


def transcode(url: str) -> TranscodedPage:
    """Fetch and transcode ``url`` into a :class:`TranscodedPage`.

    Raises :class:`TranscodeError` on any failure before the body is ready.
    The SSRF guard runs first — before robots.txt, before any network IO —
    so a URL pointing at a private address fails fast with a 400.
    """
    try:
        validate_url(url)
    except UnsafeURLError as e:
        raise TranscodeError(f"blocked: {e}", status_hint=400) from e

    if not _can_fetch(url):
        raise TranscodeError("blocked by remote robots.txt", status_hint=403)

    try:
        result = safe_get(
            url,
            max_bytes=config.reader_max_bytes,
            accept="text/html,application/xhtml+xml,*/*;q=0.8",
            user_agent=config.reader_user_agent,
        )
    except FetchError as e:
        raise TranscodeError(str(e), status_hint=e.status_hint) from e

    # Binary / non-HTML content: don't transcode. The reader.html template
    # renders a stub with a "Open original" link.
    if not result.content_type.startswith("text/html") and not result.content_type.startswith(
        "application/xhtml"
    ):
        return TranscodedPage(
            title=url,
            body_html="",
            original_url=result.final_url,
            is_html=False,
            upstream_content_type=result.content_type or "application/octet-stream",
            upstream_size=len(result.content),
        )

    text = _decode(result.content, result.content_type)

    try:
        doc = Document(text)
        title = (doc.short_title() or "").strip() or result.final_url
        summary_html = doc.summary(html_partial=True)
    except Exception as e:  # noqa: BLE001 — Readability raises a grab bag
        raise TranscodeError(f"readability failure: {type(e).__name__}", status_hint=502) from e

    soup = BeautifulSoup(summary_html, "lxml")
    _clean(soup, result.final_url)

    body_html = soup.decode(formatter="html")
    return TranscodedPage(
        title=title,
        body_html=body_html,
        original_url=result.final_url,
        is_html=True,
        upstream_content_type=result.content_type,
        upstream_size=len(result.content),
    )


__all__ = ("TranscodeError", "TranscodedPage", "transcode")
