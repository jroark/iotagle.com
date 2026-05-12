"""GET /read?url=… — reader-mode page proxy."""

from __future__ import annotations

from flask import Blueprint, render_template, request

from iotagle.services.transcoder import TranscodeError, transcode

bp = Blueprint("reader", __name__)


@bp.get("/read")
def read():
    url = (request.args.get("url") or "").strip()
    if not url:
        return render_template(
            "error.html",
            status=400,
            message="Missing url= parameter.",
        ), 400

    try:
        page = transcode(url)
    except TranscodeError as e:
        return render_template(
            "error.html",
            status=e.status_hint,
            message=f"Could not read that page: {e}.",
        ), e.status_hint

    return render_template("read.html", page=page)
