"""Meta routes: /healthz, /robots.txt, /favicon.ico.

In production nginx serves /robots.txt and /favicon.ico directly from
``static/``. The Flask routes here exist for local development and as a
fallback if the nginx rules are ever removed.
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, send_from_directory

bp = Blueprint("meta", __name__)


@bp.get("/healthz")
def healthz():
    return Response("ok\n", mimetype="text/plain")


@bp.get("/robots.txt")
def robots():
    return send_from_directory(current_app.static_folder, "robots.txt", mimetype="text/plain")


@bp.get("/favicon.ico")
def favicon():
    return send_from_directory(current_app.static_folder, "favicon.ico", mimetype="image/x-icon")
