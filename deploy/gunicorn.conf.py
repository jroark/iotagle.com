"""Gunicorn configuration for iotagle.

Tuned for a 512 MB Lightsail instance with one CPU. Two sync workers leaves
headroom for nginx, journald, and the kernel. ``timeout`` must be larger than
the app's own 13 s worst-case fetch (5 s connect + 8 s read) so an upstream
hang surfaces as a clean error instead of a worker kill.
"""

import os

# 127.0.0.1 TCP rather than a unix socket: when systemd ``RuntimeDirectory``
# is combined with any namespace-isolation flag (``PrivateTmp``,
# ``ProtectHome``, ...), the runtime directory becomes namespace-private and
# files written to it are invisible to processes outside that namespace —
# including nginx. TCP on the loopback interface sidesteps the issue with no
# real perf cost.
bind = "127.0.0.1:8000"
workers = int(os.environ.get("IOTAGLE_WORKERS", "2"))
worker_class = "sync"
threads = 1

# Longer than the app's max upstream timeout, shorter than nginx's
# ``proxy_read_timeout 30s``.
timeout = 25
graceful_timeout = 10
keepalive = 2

# stdout / stderr go to journald via the systemd unit.
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Recycle workers periodically to release any small memory leaks in
# Pillow / lxml. 1000 requests/jitter 50 = roughly daily on this traffic.
max_requests = 1000
max_requests_jitter = 50
