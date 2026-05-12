"""GET /search — DDG-backed search with shortcut handling.

Order of operations:
1. Empty query → render home.
2. Shortcut match (``w:foo``) → 302 to ``/read?url=...``.
3. Cached or fresh DDG hit → render results.
4. Circuit open or DDG error → render the "search unavailable" page.
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request

from iotagle.services.ddg import DDGError, DDGUnavailable, search
from iotagle.services.shortcuts import maybe_shortcut

bp = Blueprint("search", __name__)


@bp.get("/search")
def do_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return render_template("home.html")

    shortcut = maybe_shortcut(q)
    if shortcut:
        # 302, not 301, so vintage browsers don't cache the bounce.
        return redirect(shortcut, code=302)

    try:
        results = search(q)
    except DDGUnavailable:
        return render_template("ddg_unavailable.html", q=q), 503
    except DDGError:
        return render_template("ddg_unavailable.html", q=q), 502

    return render_template("results.html", q=q, results=results)
