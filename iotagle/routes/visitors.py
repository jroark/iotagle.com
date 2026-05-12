"""GET /visitors — the last 100 unique browser User-Agents seen.

The point of the page is to surface unusual traffic (vintage browsers, text
browsers, weird embedded devices). Layout choices are vintage-browser-first:
single-column stacked rows, no fixed widths, ``<wbr>`` break opportunities
inserted into the UA string so it wraps on a 512×384 screen.
"""

from __future__ import annotations

import re
import time

from flask import Blueprint, current_app, render_template
from markupsafe import Markup, escape

from iotagle.services import visitors as visitors_svc
from iotagle.services.ua_classify import is_vintage

bp = Blueprint("visitors", __name__)

VISITOR_LIMIT = 100

# Characters in a UA string after which a vintage browser is *usually*
# willing to wrap a line. Inserting <wbr> after each gives modern browsers
# clean wrap points; old browsers either understand <wbr> too (rare) or
# silently ignore the unknown tag and rely on the punctuation itself.
_WBR_BREAKS = re.compile(r"([;/)\]])")


def _format_ua(ua: str) -> Markup:
    """Escape ``ua`` and inject ``<wbr>`` after every ``;``, ``/``, ``)``, ``]``.

    The escape happens *first* so any HTML in the UA string is neutralized
    before we splice in the break tags.
    """
    escaped = str(escape(ua))
    return Markup(_WBR_BREAKS.sub(r"\1<wbr>", escaped))


def _relative_time(then_seconds: int, now: int | None = None) -> str:
    """Render a unix timestamp as a short "5m ago" style string."""
    if now is None:
        now = int(time.time())
    delta = max(0, now - int(then_seconds))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    if delta < 86400 * 30:
        return f"{delta // 86400}d ago"
    return time.strftime("%Y-%m-%d", time.gmtime(then_seconds))


@bp.get("/visitors")
def index():
    db = current_app.config["VISITORS_DB_PATH"]
    try:
        rows = visitors_svc.recent(db, limit=VISITOR_LIMIT)
    except Exception:  # noqa: BLE001 — render an empty page rather than 500
        rows = []
    now = int(time.time())
    items = [
        {
            "ua_html": _format_ua(r.ua),
            "ua_plain": r.ua,
            "hits": r.hits,
            "first_seen": _relative_time(r.first_seen, now),
            "last_seen": _relative_time(r.last_seen, now),
            "last_path": r.last_path or "",
            "is_vintage": is_vintage(r.ua),
        }
        for r in rows
    ]
    return render_template("visitors.html", items=items, limit=VISITOR_LIMIT)
