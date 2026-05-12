"""Flask application factory.

Routes are organized into blueprints in ``iotagle.routes``; this module wires
them up, registers a Jinja filter for entity-encoding non-Latin-1 characters
(so Netscape 1.1 doesn't see UTF-8 mojibake), and installs minimal error
handlers that always render the HTML 3.2 error template.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, render_template
from markupsafe import Markup, escape

from iotagle.routes import home, image_proxy, meta, reader, search

_PACKAGE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _PACKAGE_DIR.parent / "static"


def _entity_encode_high(value: str) -> Markup:
    """Encode any character above U+00FF as a numeric HTML entity.

    Lets us output a single UTF-8 page that still renders correctly on
    pre-Unicode browsers (Netscape 1.1, MacWeb 2). HTML escaping for
    ``<>&"'`` runs first so this stays composable with normal autoescape.
    """
    if value is None:
        return Markup("")
    escaped = str(escape(value))
    out_chars: list[str] = []
    for ch in escaped:
        cp = ord(ch)
        if cp > 0xFF:
            out_chars.append(f"&#{cp};")
        else:
            out_chars.append(ch)
    return Markup("".join(out_chars))


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=str(_STATIC_DIR),
        static_url_path="/static",
    )

    # No flash messages, no signed cookies in v1. Set a deterministic key so
    # Flask doesn't shout in development, but the value isn't security-critical
    # because we never sign anything.
    app.config.setdefault("SECRET_KEY", "iotagle-not-used-for-signing")

    app.jinja_env.filters["entity_encode_high"] = _entity_encode_high

    app.register_blueprint(home.bp)
    app.register_blueprint(search.bp)
    app.register_blueprint(reader.bp)
    app.register_blueprint(image_proxy.bp)
    app.register_blueprint(meta.bp)

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", status=404, message="That page isn't here."), 404

    @app.errorhandler(500)
    def server_error(_e):
        return render_template(
            "error.html", status=500, message="Something went wrong inside iotagle."
        ), 500

    return app


# Module-level instance so ``wsgi:app`` works without a factory call.
app = create_app()


__all__ = ("app", "create_app")
