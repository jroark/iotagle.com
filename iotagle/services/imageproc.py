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
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from iotagle.config import config
from iotagle.services.fetcher import FetchError, safe_get

# Single transparent GIF byte sequence used as the universal fallback.
TRANSPARENT_GIF_1X1 = bytes.fromhex(
    "47494638396101000100800000ffffff00000021f90401000000002c00000000" "01000100000202444c003b"
)

Mode = Literal["color", "gray", "1bit"]
ALLOWED_MODES: frozenset[str] = frozenset({"color", "gray", "1bit"})

# Sanity bounds on the width parameter. Anything wider than ~1024 px on a
# vintage screen is wasted bytes; anything below 16 produces an unreadable
# thumbnail.
MIN_WIDTH = 16
MAX_WIDTH = 1024
DEFAULT_WIDTH = 512


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

    try:
        # ``verify`` makes sure the file isn't truncated, then we reopen
        # because verify leaves the image in a not-usable state.
        with Image.open(BytesIO(result.content)) as probe:
            probe.verify()
        img = Image.open(BytesIO(result.content))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return _fallback()

    try:
        img = ImageOps.exif_transpose(img) or img
        # ``thumbnail`` only shrinks, never enlarges. The height cap of
        # width*4 keeps comically tall screenshots from blowing past the
        # encoder buffer.
        if img.width > target_width:
            img.thumbnail((target_width, target_width * 4), Image.Resampling.LANCZOS)
        return _encode(img, final_mode)
    except (OSError, ValueError):
        return _fallback()


__all__ = (
    "ALLOWED_MODES",
    "DEFAULT_WIDTH",
    "ImageOutput",
    "MAX_WIDTH",
    "MIN_WIDTH",
    "Mode",
    "process",
)
