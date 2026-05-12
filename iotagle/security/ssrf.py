"""SSRF guard for outbound fetches.

Every URL accepted by ``/read`` or ``/image`` flows through :func:`validate_url`.
The guard enforces three things, in order:

1. The URL itself: absolute, scheme in ``{http, https}``, port in an allowlist.
2. The hostname's *resolved* addresses: every result from ``getaddrinfo`` is
   checked, so dual-stack hosts and DNS rebinding tricks can't slip a private
   address through alongside a public one.
3. The address-range blocklist: RFC1918, loopback, link-local (notably the
   AWS instance metadata service at ``169.254.169.254``), CGNAT, multicast,
   broadcast, and the equivalent IPv6 ranges.

There is a residual DNS TOCTOU gap: ``requests`` resolves the hostname again
when it makes the request, so a malicious resolver could return a public IP
the first time and a private IP the second. Closing the gap requires pinning
the validated IP into a custom ``HTTPAdapter``. Deferred to v2; documented in
the plan's Risks section.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

# Schemes we are willing to follow.
ALLOWED_SCHEMES = frozenset({"http", "https"})

# Ports we'll connect to. ``None`` means "scheme default" (80 or 443).
ALLOWED_PORTS = frozenset({None, 80, 443, 8000, 8080, 8443})

# IPv4 blocklist. Anything matching any of these is rejected.
_BLOCKED_V4: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("0.0.0.0/8"),  # "this network"
    ipaddress.IPv4Network("10.0.0.0/8"),  # RFC1918
    ipaddress.IPv4Network("100.64.0.0/10"),  # CGNAT (RFC6598)
    ipaddress.IPv4Network("127.0.0.0/8"),  # loopback
    ipaddress.IPv4Network("169.254.0.0/16"),  # link-local; includes 169.254.169.254 AWS IMDS
    ipaddress.IPv4Network("172.16.0.0/12"),  # RFC1918
    ipaddress.IPv4Network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.IPv4Network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.IPv4Network("192.168.0.0/16"),  # RFC1918
    ipaddress.IPv4Network("198.18.0.0/15"),  # benchmarking
    ipaddress.IPv4Network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.IPv4Network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.IPv4Network("224.0.0.0/4"),  # multicast
    ipaddress.IPv4Network("240.0.0.0/4"),  # reserved (includes 255.255.255.255 broadcast)
)

# IPv6 blocklist. ``::ffff:0:0/96`` is the IPv4-mapped range — addresses there
# are re-checked as IPv4 inside :func:`_is_blocked_address` so the v4 blocklist
# still applies (e.g. ``::ffff:10.0.0.1`` is rejected).
_BLOCKED_V6: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::/128"),  # unspecified
    ipaddress.IPv6Network("::1/128"),  # loopback
    ipaddress.IPv6Network("fc00::/7"),  # unique local addresses
    ipaddress.IPv6Network("fe80::/10"),  # link-local
    ipaddress.IPv6Network("ff00::/8"),  # multicast
    ipaddress.IPv6Network("2001:db8::/32"),  # documentation
    ipaddress.IPv6Network("64:ff9b::/96"),  # NAT64
    ipaddress.IPv6Network("100::/64"),  # discard prefix
)


class UnsafeURLError(ValueError):
    """Raised when a URL fails any SSRF check.

    The message is safe to surface to users — it never includes resolved IPs,
    so we don't leak internal topology to a caller probing for it.
    """


@dataclass(frozen=True)
class ValidatedURL:
    """A URL that has cleared every SSRF check.

    ``addresses`` is the set of IPs that resolved cleanly. v2 will hand these
    to a custom ``HTTPAdapter`` to close the DNS TOCTOU window.
    """

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if ``ip`` is in any blocked range.

    IPv4-mapped IPv6 addresses (``::ffff:a.b.c.d``) are unwrapped and re-tested
    as IPv4 so callers can't bypass the v4 blocklist by encoding addresses in
    v6 form.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_blocked_address(ip.ipv4_mapped)

    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _BLOCKED_V4)
    return any(ip in net for net in _BLOCKED_V6)


def _resolve(host: str) -> tuple[str, ...]:
    """Resolve ``host`` to every IP ``getaddrinfo`` returns.

    Raises :class:`UnsafeURLError` if resolution fails — we don't want callers
    to distinguish "doesn't exist" from "blocked" because that's an
    information leak when probing internal hosts.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeURLError("hostname could not be resolved") from e

    addrs: list[str] = []
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            addrs.append(addr)
    if not addrs:
        raise UnsafeURLError("hostname resolved to no addresses")
    return tuple(addrs)


def _parse_host(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse a string IP, stripping IPv6 zone identifiers (``fe80::1%eth0``)."""
    if "%" in addr:
        addr = addr.split("%", 1)[0]
    return ipaddress.ip_address(addr)


def validate_url(url: str) -> ValidatedURL:  # noqa: PLR0912 — each branch closes a distinct SSRF hole
    """Validate ``url`` against every SSRF check.

    Raises :class:`UnsafeURLError` with a generic message on any failure.
    Returns a :class:`ValidatedURL` on success.

    The branch count is intentional: every ``if`` here is a distinct
    rejection path with its own test case in ``tests/test_ssrf.py``.
    Collapsing them into a table lookup would hurt readability without
    changing semantics.
    """
    if not isinstance(url, str) or not url:
        raise UnsafeURLError("empty url")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError("scheme not allowed")
    if not parsed.netloc or not parsed.hostname:
        raise UnsafeURLError("url must be absolute")

    # Userinfo (``user:pass@host``) is a phishing/log-leak vector and never
    # legitimately needed for the kinds of pages we proxy.
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("userinfo not allowed in url")

    try:
        port = parsed.port  # may raise ValueError on a malformed authority
    except ValueError as e:
        raise UnsafeURLError("invalid port") from e
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError("port not allowed")

    host = parsed.hostname

    # If the host is itself an IP literal, skip DNS and validate directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _is_blocked_address(literal):
            raise UnsafeURLError("address in blocked range")
        addresses: tuple[str, ...] = (str(literal),)
    else:
        addresses = _resolve(host)
        for addr in addresses:
            ip = _parse_host(addr)
            if _is_blocked_address(ip):
                raise UnsafeURLError("address in blocked range")

    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    return ValidatedURL(
        url=url,
        scheme=scheme,
        host=host,
        port=effective_port,
        addresses=addresses,
    )


def is_safe_url(url: str) -> bool:
    """Convenience boolean wrapper for callers that don't need the resolved IPs."""
    try:
        validate_url(url)
    except UnsafeURLError:
        return False
    return True


__all__ = (
    "ALLOWED_PORTS",
    "ALLOWED_SCHEMES",
    "UnsafeURLError",
    "ValidatedURL",
    "is_safe_url",
    "validate_url",
)
