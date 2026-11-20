"""Gunicorn configuration for iotagle.

Tuned for a 1 GB Lightsail micro instance (2 vCPU burstable). Two processes leave
headroom for nginx, journald, and the kernel.

Threads: almost all request time is spent waiting on upstream sites (up to
``config.total_timeout`` = 12 s per fetch). With plain sync workers, two slow
/read requests would occupy the whole server. ``gthread`` with 4 threads
per worker gives 8 concurrent requests for little extra memory, because the
memory-heavy path (image decode) is limited to one at a time per process in
``iotagle/services/imageproc.py``. Shared in-process state (DDG cache and
circuit breaker, visitors init set) is already guarded by locks.

``timeout`` must be larger than the app's worst-case fetch (total_timeout
plus one trailing read_timeout = 20 s) so an upstream hang surfaces as a
clean error instead of a worker kill. For gthread, ``timeout`` is the
worker heartbeat; it still kills a process whose threads all hang.
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
worker_class = "gthread"
threads = int(os.environ.get("IOTAGLE_THREADS", "4"))

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

# gunicorn 24+ opens a runtime-control unix socket (``gunicornc``) under the
# service user's home by default. We don't use it, the home directory is
# read-only under the systemd sandbox, and it's one more local management
# surface, so turn it off.
control_socket_disable = True
