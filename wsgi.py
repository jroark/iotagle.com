"""WSGI entrypoint for gunicorn: ``gunicorn -c deploy/gunicorn.conf.py wsgi:app``."""

from iotagle.app import app  # noqa: F401  — re-exported for gunicorn

__all__ = ("app",)
