"""Image proxy: fetch, decode, downscale, optionally dither, re-encode.

Why we re-encode everything: vintage browsers can choke on modern PNG
features (color management blocks, 16-bit channels, animated APNG), don't
understand WebP/AVIF, and have wildly varying JPEG decoder quality. By
pushing everything through Pillow we hand them a small, predictable file.

Three output modes:

- ``color`` → low-quality non-progressive JPEG. Smallest for photos.
- ``gray`` → 8-bit GIF. Good for vintage grayscale Macs.
- ``1bit`` → Floyd–Steinberg dithered 1-bit GIF. Best for compact Macs.

Failures collapse to a 1×1 transparent GIF so a broken image never breaks
the page that contains it.

Memory safety (the box has ``MemoryMax=350M`` shared by every worker/thread):

- Only a short allowlist of decoders is reachable (:data:`ALLOWED_FORMATS`).
  Most historical Pillow CVEs live in exotic formats nobody embeds on the web.
- JPEGs are opened with :meth:`Image.draft`, so libjpeg decodes directly at
  1/2–1/8 scale instead of materializing a full-size bitmap.
- The *post-draft* pixel count is capped at :data:`MAX_DECODE_PIXELS` before
  any pixel data is decoded.
- :data:`_DECODE_SLOTS` allows one decode+encode per process at a time, so
  gthread concurrency can't multiply the peak.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from iotagle.config import config
from iotagle.services.fetcher import FetchError, safe_get

# Single transparent GIF byte sequence used as the universal fallback.
TRANSPARENT_GIF_1X1 = bytes.fromhex(
    "47494638396101000100800000ffffff00000021f90401000000002c0000000001000100000202444c003b"
)

Mode = Literal["color", "gray", "1bit"]
ALLOWED_MODES: frozenset[str] = frozenset({"color", "gray", "1bit"})

# Sanity bounds on the width parameter. Anything wider than ~1024 px on a
# vintage screen is wasted bytes; anything below 16 produces an unreadable
# thumbnail.
MIN_WIDTH = 16
MAX_WIDTH = 1024
DEFAULT_WIDTH = 512

# Decoders we're willing to run on attacker-supplied bytes.
ALLOWED_FORMATS: tuple[str, ...] = ("JPEG", "PNG", "GIF", "WEBP", "BMP")

# Hard cap on decoded pixels (checked after JPEG draft scaling). 12 MP RGBA is
# ~48 MB; with one decode slot per worker that keeps the worst case well under
# the systemd MemoryMax.
MAX_DECODE_PIXELS = 12_000_000

# Pillow's own header-time bomb check is a backstop only; it raises
# DecompressionBombError above 2x this value. Set it loosely so a large JPEG
# can still be opened and draft-scaled below MAX_DECODE_PIXELS.
Image.MAX_IMAGE_PIXELS = 64_000_000

# One concurrent decode per process. Waiting longer than this returns the
# fallback rather than queueing indefinitely behind a slow neighbor.
_DECODE_SLOTS = threading.BoundedSemaphore(1)
_DECODE_SLOT_WAIT_SECONDS = 10.0

_DECODE_ERRORS = (
    UnidentifiedImageError,
    Image.DecompressionBombError,
    OSError,
    ValueError,
    SyntaxError,
)


@dataclass(frozen=True)
class ImageOutput:
    """Encoded image ready to be sent back to the client."""

    body: bytes
    content_type: str
    cache_seconds: int = 86400


def _normalize_width(raw: object) -> int:
    """Parse ``?w=``: clamp to [MIN_WIDTH, MAX_WIDTH], default on bad input."""
    if raw is None or raw == "":
        return DEFAULT_WIDTH
    try:
        w = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_WIDTH
    if w < MIN_WIDTH:
        return MIN_WIDTH
    if w > MAX_WIDTH:
        return MAX_WIDTH
    return w


def _normalize_mode(raw: object) -> Mode:
    if isinstance(raw, str) and raw.lower() in ALLOWED_MODES:
        return raw.lower()  # type: ignore[return-value]
    return "color"


def _fallback() -> ImageOutput:
    """Universal "couldn't decode this" response."""
    return ImageOutput(
        body=TRANSPARENT_GIF_1X1,
        content_type="image/gif",
        cache_seconds=0,
    )


def _encode(img: Image.Image, mode: Mode) -> ImageOutput:
    """Apply the per-mode conversion and encode to the appropriate format."""
    buf = BytesIO()
    if mode == "color":
        rgb = img.convert("RGB")
        rgb.save(buf, format="JPEG", quality=60, optimize=True, progressive=False)
        return ImageOutput(body=buf.getvalue(), content_type="image/jpeg")
    if mode == "gray":
        gray = img.convert("L")
        gray.save(buf, format="GIF", optimize=True)
        return ImageOutput(body=buf.getvalue(), content_type="image/gif")
    # 1bit — Floyd–Steinberg dither.
    gray = img.convert("L")
    bw = gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    bw.save(buf, format="GIF", optimize=True)
    return ImageOutput(body=buf.getvalue(), content_type="image/gif")


def process(url: str, *, mode: object = "color", width: object = None) -> ImageOutput:
    """Fetch and transform ``url`` into a vintage-friendly image.

    Never raises: any decode or transport failure produces the 1×1 fallback.
    """
    final_mode = _normalize_mode(mode)
    target_width = _normalize_width(width)

    try:
        result = safe_get(
            url,
            max_bytes=config.image_max_bytes,
            accept="image/*",
        )
    except FetchError:
        return _fallback()

    # The Content-Type may be missing or wrong on some hosts. ``Image.open``
    # sniffs the file, so we don't strictly need to trust the header — but a
    # bare "text/html" usually means the server returned an error page, and
    # we don't want to feed that to Pillow.
    if result.content_type and not result.content_type.startswith("image/"):
        return _fallback()

    if not _DECODE_SLOTS.acquire(timeout=_DECODE_SLOT_WAIT_SECONDS):
        return _fallback()
    try:
        return _decode_and_encode(result.content, final_mode, target_width)
    finally:
        _DECODE_SLOTS.release()


def _decode_and_encode(data: bytes, mode: Mode, target_width: int) -> ImageOutput:
    try:
        # ``verify`` makes sure the file isn't truncated, then we reopen
        # because verify leaves the image in a not-usable state.
        with Image.open(BytesIO(data), formats=ALLOWED_FORMATS) as probe:
            probe.verify()
        img = Image.open(BytesIO(data), formats=ALLOWED_FORMATS)
        # JPEG only (no-op elsewhere): pick the largest DCT scale that still
        # leaves the image at least ``target_width`` wide. Must run before
        # ``load``. A height of 1 means "don't constrain on height".
        if img.width > target_width:
            img.draft(None, (target_width, 1))
        # ``size`` comes from the header (adjusted by draft), so this check
        # runs before any pixel data is decoded.
        if img.width * img.height > MAX_DECODE_PIXELS:
            return _fallback()
        img.load()
    except _DECODE_ERRORS:
        return _fallback()

    try:
        img = ImageOps.exif_transpose(img) or img
        # ``thumbnail`` only shrinks, never enlarges. The height cap of
        # width*4 keeps comically tall screenshots from blowing past the
        # encoder buffer.
        if img.width > target_width:
            img.thumbnail((target_width, target_width * 4), Image.Resampling.LANCZOS)
        return _encode(img, mode)
    except (OSError, ValueError):
        return _fallback()


__all__ = (
    "ALLOWED_FORMATS",
    "ALLOWED_MODES",
    "DEFAULT_WIDTH",
    "ImageOutput",
    "MAX_DECODE_PIXELS",
    "MAX_WIDTH",
    "MIN_WIDTH",
    "Mode",
    "process",
)
