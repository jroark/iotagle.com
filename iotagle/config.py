"""Runtime configuration, driven by environment variables.

Anything that might want to vary between dev and prod (timeouts, size caps,
the User-Agent pool, the DDG endpoint) lives here. Modules import the
``config`` object, not raw env vars, so tests can override values without
touching ``os.environ``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Real desktop UAs. We rotate through these for outbound requests so a single
# fingerprint doesn't trigger DDG's anti-scraping. Update occasionally as
# browsers move on.
DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) " "Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) " "Gecko/20100101 Firefox/124.0",
)

# Identifies our reader when fetching pages to transcode. Distinct from the UA
# pool above so site operators see a clean trail in their logs.
READER_USER_AGENT = "iotagle-reader/0.1 (+http://iotagle.com/about)"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Immutable settings snapshot."""

    env: str = field(default_factory=lambda: _env("IOTAGLE_ENV", "dev"))

    # DuckDuckGo Lite endpoint. Override with IOTAGLE_DDG_URL to point tests
    # at a local fixture server, or to swap to lite.duckduckgo.com/lite if
    # they ever resurrect the bare-domain alias.
    ddg_url: str = field(
        default_factory=lambda: _env(
            "IOTAGLE_DDG_URL",
            "https://lite.duckduckgo.com/lite/",
        ),
    )
    ddg_region: str = field(default_factory=lambda: _env("IOTAGLE_DDG_REGION", "us-en"))
    ddg_cache_ttl_seconds: int = field(
        default_factory=lambda: _env_int("IOTAGLE_DDG_CACHE_TTL", 600),
    )
    ddg_cache_max: int = field(
        default_factory=lambda: _env_int("IOTAGLE_DDG_CACHE_MAX", 512),
    )
    ddg_circuit_open_seconds: int = field(
        default_factory=lambda: _env_int("IOTAGLE_DDG_CIRCUIT_SECONDS", 60),
    )

    # Reader proxy limits. 2 MB is enough for almost any real article and small
    # enough that a single worker can't be tied up shipping a giant PDF.
    reader_max_bytes: int = field(
        default_factory=lambda: _env_int("IOTAGLE_READER_MAX_BYTES", 2_000_000),
    )
    # Image proxy gets a larger cap because some legitimate hi-res PNGs are big.
    image_max_bytes: int = field(
        default_factory=lambda: _env_int("IOTAGLE_IMAGE_MAX_BYTES", 5_000_000),
    )

    # Per-request HTTP timeouts (seconds). Sum must stay under gunicorn's
    # worker timeout (25 s in deploy/gunicorn.conf.py).
    connect_timeout: float = 5.0
    read_timeout: float = 8.0

    # Max redirects to follow. Each one re-runs the SSRF guard.
    max_redirects: int = 3

    user_agents: tuple[str, ...] = DEFAULT_USER_AGENTS
    reader_user_agent: str = READER_USER_AGENT

    # SQLite path for the recent-visitors log. /var/lib/iotagle is created
    # in deploy/bootstrap.sh and owned by the iotagle user.
    visitors_db_path: str = field(
        default_factory=lambda: _env("IOTAGLE_VISITORS_DB", "/var/lib/iotagle/visitors.db"),
    )

    # Flask SECRET_KEY. Not used for signing in v1 (no sessions, no flash), but
    # set to a unique random value in prod via /etc/iotagle.env so the default
    # fallback never reaches production.
    secret_key: str = field(
        default_factory=lambda: _env("IOTAGLE_SECRET_KEY", "iotagle-dev-key-not-for-production"),
    )


# Module-level singleton. Routes and services import this; tests can monkeypatch
# attributes when they need a specific value.
config = Config()
