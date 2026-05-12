"""SSRF guard tests.

The guard's job is to fail closed. Each test pins down one rejection path so a
regression won't quietly turn into a private-network reach.
"""

from __future__ import annotations

import pytest

from iotagle.security.ssrf import (
    ALLOWED_PORTS,
    UnsafeURLError,
    is_safe_url,
    validate_url,
)

# --- Scheme & syntax ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://example.com/",
        "gopher://example.com/",
        "data:text/html,hi",
        "mailto:foo@example.com",
        "//example.com/no-scheme",
        "http://",  # no host
        "http:///path",  # no host
        "ht!tp://example.com/",  # invalid scheme chars (urlparse still parses; we reject)
    ],
)
def test_rejects_bad_scheme_or_syntax(url):
    assert not is_safe_url(url)


def test_rejects_non_string():
    assert not is_safe_url(None)  # type: ignore[arg-type]


# --- Port allowlist ----------------------------------------------------------


@pytest.mark.parametrize("port", [22, 25, 3306, 5432, 6379, 11211, 9000, 8081])
def test_rejects_disallowed_ports(port, monkeypatch):
    # We must avoid DNS for these — point getaddrinfo at a known-public IP.
    monkeypatch.setattr(
        "iotagle.security.ssrf.socket.getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    assert not is_safe_url(f"http://example.com:{port}/")


@pytest.mark.parametrize("port", sorted(p for p in ALLOWED_PORTS if p is not None))
def test_accepts_allowlisted_ports(port, monkeypatch):
    monkeypatch.setattr(
        "iotagle.security.ssrf.socket.getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    assert is_safe_url(f"http://example.com:{port}/path")


# --- Userinfo ---------------------------------------------------------------


def test_rejects_userinfo(monkeypatch):
    monkeypatch.setattr(
        "iotagle.security.ssrf.socket.getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    assert not is_safe_url("http://user:pass@example.com/")
    assert not is_safe_url("http://user@example.com/")


# --- IP literals: blocked ranges --------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Loopback
        "http://127.0.0.1/",
        "http://127.1.2.3/",
        "http://[::1]/",
        # RFC1918
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.1.1/",
        # Link-local — including the AWS instance metadata service
        "http://169.254.169.254/",
        "http://169.254.0.1/",
        "http://[fe80::1]/",
        # CGNAT
        "http://100.64.0.1/",
        # Multicast / broadcast / reserved
        "http://224.0.0.1/",
        "http://255.255.255.255/",
        "http://240.0.0.1/",
        # "this network"
        "http://0.0.0.0/",
        # IPv6 ULA
        "http://[fc00::1]/",
        "http://[fd00::1]/",
        # Documentation ranges (lab/test, but still not somewhere we should reach)
        "http://192.0.2.1/",
        "http://198.51.100.1/",
        "http://203.0.113.1/",
        # IPv4-mapped IPv6 must unwrap and apply v4 blocklist
        "http://[::ffff:10.0.0.1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
    ],
)
def test_rejects_blocked_ip_literals(url):
    assert not is_safe_url(url)


def test_rejects_blocked_with_validate_url_raises():
    with pytest.raises(UnsafeURLError) as exc:
        validate_url("http://169.254.169.254/latest/meta-data/")
    # Message must not leak the resolved address back to the caller.
    assert "169.254" not in str(exc.value)


# --- IP literals: public addresses are accepted -----------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://93.184.216.34/",  # example.com
        "https://1.1.1.1/",  # cloudflare
        "https://8.8.8.8/",  # google dns
        "http://[2606:2800:220:1:248:1893:25c8:1946]/",  # example.com v6
    ],
)
def test_accepts_public_ip_literals(url):
    result = validate_url(url)
    assert result.scheme in {"http", "https"}
    assert len(result.addresses) == 1


# --- DNS resolution paths ----------------------------------------------------


def _gai(addrs):
    """Return a getaddrinfo replacement that yields ``addrs`` (list of strings)."""

    def _impl(*_args, **_kwargs):
        return [(0, 0, 0, "", (a, 0)) for a in addrs]

    return _impl


def test_dns_to_public_address_allowed(monkeypatch):
    monkeypatch.setattr(
        "iotagle.security.ssrf.socket.getaddrinfo",
        _gai(["93.184.216.34"]),
    )
    result = validate_url("http://example.com/path")
    assert result.host == "example.com"
    assert result.port == 80
    assert result.addresses == ("93.184.216.34",)


def test_dns_to_private_address_blocked(monkeypatch):
    monkeypatch.setattr(
        "iotagle.security.ssrf.socket.getaddrinfo",
        _gai(["10.20.30.40"]),
    )
    with pytest.raises(UnsafeURLError):
        validate_url("http://intranet.example/")


def test_dns_dual_stack_with_one_private_blocked(monkeypatch):
    """If a host resolves to a public *and* a private address, reject it.

    Anything else lets a malicious resolver smuggle in a private address
    alongside a public one.
    """
    monkeypatch.setattr(
        "iotagle.security.ssrf.socket.getaddrinfo",
        _gai(["93.184.216.34", "10.0.0.1"]),
    )
    with pytest.raises(UnsafeURLError):
        validate_url("http://dual.example/")


def test_dns_failure_rejected_without_leaking(monkeypatch):
    import socket as _socket

    def _boom(*_a, **_k):
        raise _socket.gaierror("nope")

    monkeypatch.setattr("iotagle.security.ssrf.socket.getaddrinfo", _boom)
    with pytest.raises(UnsafeURLError) as exc:
        validate_url("http://nonexistent.invalid/")
    # Don't differentiate "blocked" vs "doesn't resolve" — same surface message.
    msg = str(exc.value).lower()
    assert "nope" not in msg


def test_dns_empty_result_rejected(monkeypatch):
    monkeypatch.setattr("iotagle.security.ssrf.socket.getaddrinfo", _gai([]))
    with pytest.raises(UnsafeURLError):
        validate_url("http://no-addrs.example/")


# --- ValidatedURL shape ------------------------------------------------------


def test_validated_url_fills_defaults(monkeypatch):
    monkeypatch.setattr(
        "iotagle.security.ssrf.socket.getaddrinfo",
        _gai(["93.184.216.34"]),
    )
    v = validate_url("https://example.com/path?q=1")
    assert v.scheme == "https"
    assert v.host == "example.com"
    assert v.port == 443
    assert v.addresses == ("93.184.216.34",)


def test_validated_url_explicit_port_preserved(monkeypatch):
    monkeypatch.setattr(
        "iotagle.security.ssrf.socket.getaddrinfo",
        _gai(["93.184.216.34"]),
    )
    v = validate_url("http://example.com:8080/")
    assert v.port == 8080
