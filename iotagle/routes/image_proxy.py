"""GET /image?url=&mode=&w= — image fetch + downscale."""

from __future__ import annotations

from flask import Blueprint, Response, request

from iotagle.services.imageproc import process

bp = Blueprint("image_proxy", __name__)


@bp.get("/image")
def image():
    url = (request.args.get("url") or "").strip()
    if not url:
        # Return the fallback 1x1 GIF rather than 400 — a broken <img> inside
        # a transcoded page should still render the surrounding text.
        return Response(b"", status=400, mimetype="text/plain")

    mode = request.args.get("mode") or "color"
    width = request.args.get("w")

    out = process(url, mode=mode, width=width)
    headers = {}
    if out.cache_seconds > 0:
        headers["Cache-Control"] = f"public, max-age={out.cache_seconds}"
    else:
        headers["Cache-Control"] = "no-store"
    return Response(out.body, mimetype=out.content_type, headers=headers)
