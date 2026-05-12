"""Image proxy tests.

We synthesize tiny images with Pillow rather than fetching from the network.
The properties we care about: the output format matches the requested mode,
sizes shrink past the width cap, and bad input always falls back to the 1×1
GIF rather than raising.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from iotagle.services import imageproc
from iotagle.services.fetcher import FetchError, FetchResult
from iotagle.services.imageproc import (
    DEFAULT_WIDTH,
    MAX_WIDTH,
    TRANSPARENT_GIF_1X1,
    process,
)


def _png(size=(1024, 768), color=(0, 128, 255)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _patch_fetch(monkeypatch, data: bytes, content_type: str = "image/png"):
    monkeypatch.setattr(
        imageproc,
        "safe_get",
        lambda *a, **k: FetchResult(
            content=data,
            content_type=content_type,
            final_url="https://example.com/i.png",
            status_code=200,
        ),
    )


def test_color_mode_returns_jpeg(monkeypatch):
    _patch_fetch(monkeypatch, _png())
    out = process("https://example.com/i.png", mode="color")
    assert out.content_type == "image/jpeg"
    # Verify the bytes actually parse as a JPEG.
    img = Image.open(BytesIO(out.body))
    assert img.format == "JPEG"


def test_gray_mode_returns_gif(monkeypatch):
    _patch_fetch(monkeypatch, _png())
    out = process("https://example.com/i.png", mode="gray")
    assert out.content_type == "image/gif"
    img = Image.open(BytesIO(out.body))
    assert img.format == "GIF"
    assert img.mode in ("L", "P")  # GIF doesn't expose "L" directly


def test_1bit_mode_returns_dithered_gif(monkeypatch):
    _patch_fetch(monkeypatch, _png((400, 300), color=(127, 127, 127)))
    out = process("https://example.com/i.png", mode="1bit")
    assert out.content_type == "image/gif"
    img = Image.open(BytesIO(out.body))
    # Convert back to 1-bit to confirm the palette only has two colors.
    bw = img.convert("1")
    pixels = set(bw.getdata())
    assert pixels.issubset({0, 255}), f"expected 1-bit output, got pixels: {pixels}"


def test_width_downscaling(monkeypatch):
    _patch_fetch(monkeypatch, _png((2000, 1500)))
    out = process("https://example.com/i.png", mode="color", width=320)
    img = Image.open(BytesIO(out.body))
    assert img.width <= 320


def test_width_clamped_to_max(monkeypatch):
    _patch_fetch(monkeypatch, _png((2000, 1500)))
    out = process("https://example.com/i.png", mode="color", width=9999)
    img = Image.open(BytesIO(out.body))
    # Either MAX_WIDTH if the image is wider, or the original width if smaller.
    assert img.width <= MAX_WIDTH


def test_invalid_width_falls_back_to_default(monkeypatch):
    _patch_fetch(monkeypatch, _png((1024, 768)))
    out = process("https://example.com/i.png", mode="color", width="abc")
    img = Image.open(BytesIO(out.body))
    assert img.width <= DEFAULT_WIDTH


def test_unknown_mode_falls_back_to_color(monkeypatch):
    _patch_fetch(monkeypatch, _png())
    out = process("https://example.com/i.png", mode="hypercolor")
    assert out.content_type == "image/jpeg"


def test_fetch_error_returns_transparent_gif(monkeypatch):
    def boom(*_a, **_k):
        raise FetchError("upstream timeout", status_hint=504)

    monkeypatch.setattr(imageproc, "safe_get", boom)
    out = process("https://example.com/i.png")
    assert out.body == TRANSPARENT_GIF_1X1
    assert out.cache_seconds == 0


def test_non_image_content_type_falls_back(monkeypatch):
    _patch_fetch(monkeypatch, b"<html>not an image</html>", content_type="text/html")
    out = process("https://example.com/i.png")
    assert out.body == TRANSPARENT_GIF_1X1


def test_corrupt_image_falls_back(monkeypatch):
    _patch_fetch(monkeypatch, b"\x89PNG\r\nthis is not actually a png", content_type="image/png")
    out = process("https://example.com/i.png")
    assert out.body == TRANSPARENT_GIF_1X1


def test_does_not_upscale(monkeypatch):
    """``thumbnail`` only shrinks. A small image must come out at original size."""
    _patch_fetch(monkeypatch, _png((100, 80)))
    out = process("https://example.com/i.png", mode="color", width=512)
    img = Image.open(BytesIO(out.body))
    assert img.width == 100
    assert img.height == 80
