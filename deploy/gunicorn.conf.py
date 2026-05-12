"""Gunicorn configuration for iotagle.

Tuned for a 512 MB Lightsail instance with one CPU. Two sync workers leaves
headroom for nginx, journald, and the kernel. ``timeout`` must be larger than
the app's own 13 s worst-case fetch (5 s connect + 8 s read) so an upstream
hang surfaces as a clean error instead of a worker kill.
"""

import os

bind = "unix:/run/iotagle/iotagle.sock"
workers = int(os.environ.get("IOTAGLE_WORKERS", "2"))
worker_class = "sync"
threads = 1

# Longer than the app's max upstream timeout, shorter than nginx's
# ``proxy_read_timeout 30s``.
timeout = 25
graceful_timeout = 10
keepalive = 2

# 0o007 lets the www-data group read the socket while keeping it private from
# everyone else on the box.
umask = 0o007

# stdout / stderr go to journald via the systemd unit.
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Recycle workers periodically to release any small memory leaks in
# Pillow / lxml. 1000 requests/jitter 50 = roughly daily on this traffic.
max_requests = 1000
max_requests_jitter = 50
