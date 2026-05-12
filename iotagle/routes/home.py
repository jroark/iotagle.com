"""Static-ish routes: home page and about."""

from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("home", __name__)


@bp.get("/")
def index():
    return render_template("home.html")


@bp.get("/about")
def about():
    return render_template("about.html")
