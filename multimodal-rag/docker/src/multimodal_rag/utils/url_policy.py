"""Shared URL-fetch policy (SSRF guards) for remote http(s) fetches.

Used by:

* ``dataset_manager._download_url`` — ingest-time downloads of user-supplied
  URLs (``batch-urls``, ``add_file`` with a URL);
* ``rag_system`` — media refs inside user-supplied documents (``image`` /
  ``video`` / ``audio`` keys), fetched at embed time and query time;
* ``mcp_server`` — query-time media URLs on the MCP tools.

Lives in ``utils/`` (not ``dataset_manager``) so ``rag_system`` can import it
without a circular import (``dataset_manager`` imports ``rag_system`` at
module level).  ``dataset_manager`` re-exports the public names so existing
``from multimodal_rag.dataset_manager import _check_media_url_policy`` call
sites keep working.

Configuration (read once at import):

* ``INGEST_ALLOW_HOSTS`` — comma-separated host allowlist.  An entry like
  ``.minio.svc.cluster.local`` matches the zone and subdomains.  When set it
  is authoritative: listed hosts are allowed (even when they resolve
  privately — that is how in-cluster MinIO/internal ingestions are
  permitted) and everything else is rejected.
* ``INGEST_BLOCK_PRIVATE_HOSTS`` — private/loopback/link-local block toggle,
  default on.  ``false`` restores the legacy permissive behaviour.

DNS pinning (fleet audit MED: DNS-rebinding TOCTOU): validating a URL only
checks the DNS answer at CHECK time — between the check and the actual
fetch a rebinding DNS answer can hand the fetch a private address.  The
fetch helpers therefore call :func:`validate_fetch_url`, which returns a
:class:`PinnedUrl`: the connection is made to the *validated IP* with the
original ``Host`` header (and TLS SNI) preserved, so check-time DNS =
fetch-time DNS.  When an HTTP(S) proxy is configured the pin is skipped
(the proxy performs egress DNS; rewriting would break TLS SNI behind it —
the same documented residual as the searxng port); the denylist still ran
on the check-time resolution.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

# Optional comma-separated allowlist of hosts for http(s) fetches.  Empty
# = all hosts allowed (subject to the private-range block below).
_INGEST_ALLOW_HOSTS = tuple(h.strip().lower() for h in os.environ.get("INGEST_ALLOW_HOSTS", "").split(",") if h.strip())

# Private-range block (DNS + literal-IP).  On by default so remote fetches
# cannot reach internal/loopback targets (SSRF).
_INGEST_BLOCK_PRIVATE = os.environ.get("INGEST_BLOCK_PRIVATE_HOSTS", "true").lower() in ("1", "true", "yes")


def _host_matches_allowlist(host: str) -> bool:
    host = host.lower()
    for pat in _INGEST_ALLOW_HOSTS:
        if pat.startswith("."):
            if host == pat[1:] or host.endswith(pat):
                return True
        elif host == pat:
            return True
    return False


def _host_is_private(host: str, allow_loopback: bool = False) -> bool:
    """Return True if *host* is or resolves to a private/loopback/link-local address.

    With ``allow_loopback=True`` (the query-time media policy) loopback
    addresses and the literal name ``localhost`` are *not* considered
    private: clients legitimately hand the server's own media URLs
    (``http://localhost:8000/api/datasets/...``) back to the query tools.
    """
    import ipaddress
    import socket

    # Bracketed / bare IPv6 literals must survive intact: the port-strip
    # below (for direct "host:port" callers) mangles them ("::1".rsplit →
    # ":"), which over-refused every IPv6 URL.  An IP literal is never
    # port-split; only a name with EXACTLY one colon can be "host:port".
    hostname = host.strip("[]")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if hostname.count(":") == 1:
            hostname = hostname.rsplit(":", 1)[0].strip("[]")
    if hostname == "localhost":
        return not allow_loopback
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_loopback and allow_loopback:
            return False
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
    try:
        addrinfos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True  # unresolved — safest to treat as suspicious when blocking is on
    return _addresses_private([info[4][0] for info in addrinfos], allow_loopback=allow_loopback)


def _addresses_private(addrs, allow_loopback: bool = False) -> bool:
    """True when any of the already-resolved *addrs* is private/loopback/
    link-local (classification shared by :func:`_host_is_private` and the
    DNS pin, so both judge the SAME address set)."""
    import ipaddress

    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_loopback and allow_loopback:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return True
    return False


def _check_url_policy(url: str) -> None:
    """Raise :class:`ValueError` if *url* violates the configured URL policy.

    Ingest-time variant: loopback is blocked like every other private range
    (a remote ingest URL has no business pointing at the server itself).
    """
    if not url.startswith(("http://", "https://")):
        return
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    # An explicit allowlist is authoritative: hosts not listed are rejected,
    # and listed hosts are allowed even when they resolve privately (that is
    # how in-cluster MinIO/internal ingestions are permitted).
    if _INGEST_ALLOW_HOSTS:
        if not _host_matches_allowlist(host):
            raise ValueError(
                f"URL host '{host}' is not allowed by INGEST_ALLOW_HOSTS"
                + (f"={','.join(_INGEST_ALLOW_HOSTS)}" if _INGEST_ALLOW_HOSTS else "")
            )
        return
    if _INGEST_BLOCK_PRIVATE and _host_is_private(host):
        raise ValueError(f"URL host '{host}' resolves to a private/internal address (INGEST_BLOCK_PRIVATE_HOSTS=true)")


def _check_media_url_policy(url: str) -> None:
    """Policy for *media* fetches of user-supplied http(s) refs.

    Covers both query-time media URLs (search with image/video/audio,
    ``describe_media``, ``transcribe_audio``) and media refs embedded in
    user-supplied documents (``POST /documents``, MCP ``add_memory``), which
    the server fetches at embed time and again at query time.

    Same rules as :func:`_check_url_policy` with one difference: loopback is
    allowed by default, because clients legitimately pass the server's own
    media URLs (``http://localhost:8000/api/datasets/...?token=...``) back
    to these tools.  ``INGEST_ALLOW_HOSTS`` remains authoritative when set;
    set ``INGEST_BLOCK_PRIVATE_HOSTS=false`` to disable (not recommended).
    """
    if not url.startswith(("http://", "https://")):
        return
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if _INGEST_ALLOW_HOSTS:
        if not _host_matches_allowlist(host):
            raise ValueError(
                f"URL host '{host}' is not allowed by INGEST_ALLOW_HOSTS"
                + (f"={','.join(_INGEST_ALLOW_HOSTS)}" if _INGEST_ALLOW_HOSTS else "")
            )
        return
    if _INGEST_BLOCK_PRIVATE and _host_is_private(host, allow_loopback=True):
        raise ValueError(
            f"URL host '{host}' resolves to a private/internal address "
            f"(INGEST_BLOCK_PRIVATE_HOSTS=true; add it to INGEST_ALLOW_HOSTS to permit)"
        )


# ---------------------------------------------------------------------------
# DNS-rebinding pin (check-time DNS == fetch-time DNS)
# ---------------------------------------------------------------------------

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class PinnedUrl:
    """A validated URL plus everything needed to connect to the exact IP
    that was validated.

    ``pin_active`` is False when an HTTP(S) proxy is configured: the proxy
    performs egress DNS, so rewriting the URL to the IP would break TLS SNI
    behind it; the request then goes out with the original hostname (the
    policy still ran on the check-time resolution).
    """

    url: str  # the original, validated URL
    scheme: str
    host: str  # lowercase hostname (no brackets)
    port: int
    ip: str  # the validated address to connect to ("" when pin inactive)
    pinned_url: str  # URL with the host replaced by ``ip`` (= url when inactive)
    host_header: str  # original authority for the Host header ("" when inactive)
    sni_hostname: str  # TLS SNI / cert name ("" when inactive or http)
    pin_active: bool


def _proxy_configured() -> bool:
    """True when an HTTP(S) proxy env is set (fetch egress then goes through
    the proxy, which performs its own DNS — the pin is skipped)."""
    return any(
        os.environ.get(name, "").strip()
        for name in (
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        )
    )


def validate_fetch_url(url: str, *, allow_loopback: bool = True) -> PinnedUrl:
    """Validate *url* against the fetch policy and pin it to the validated IP.

    Same deny classes as :func:`_check_media_url_policy` (pass
    ``allow_loopback=False`` for the ingest-time policy of
    :func:`_check_url_policy`), plus the rebinding pin: the host is
    resolved ONCE, the resolved address set is classified, and the returned
    :class:`PinnedUrl` connects to a member of that same set — so a DNS
    answer that changes between the check and the connect cannot reroute
    the fetch.

    Raises :class:`ValueError` on any policy violation (unresolved hosts
    included — fail-closed, matching :func:`_host_is_private`).
    """
    from ipaddress import ip_address
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(str(url).strip())
    except ValueError as e:
        raise ValueError(f"URL could not be parsed: {e}") from e
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(f"URL scheme must be http(s), got {scheme!r}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("URL has no host")
    try:
        port = parts.port
    except ValueError as e:
        raise ValueError(f"URL has a malformed port: {e}") from e
    port = port or _DEFAULT_PORTS[scheme]

    allowlisted = bool(_INGEST_ALLOW_HOSTS) and _host_matches_allowlist(host)
    if _INGEST_ALLOW_HOSTS and not allowlisted:
        raise ValueError(
            f"URL host '{host}' is not allowed by INGEST_ALLOW_HOSTS" + f"={','.join(_INGEST_ALLOW_HOSTS)}"
        )

    literal = False
    try:
        ip_address(host)
        literal = True  # nothing to resolve — no rebinding possible
    except ValueError:
        pass

    if literal or _proxy_configured():
        # Literal IP: the URL itself is the address (no name to rebind).
        # Proxy: egress DNS belongs to the proxy (pin skipped — the same
        # documented residual as the searxng port); the check-time
        # resolution is still classified below.
        if not allowlisted and _INGEST_BLOCK_PRIVATE and _host_is_private(host, allow_loopback=allow_loopback):
            _raise_private_host_error(host, allow_loopback)
        return PinnedUrl(
            url=str(url).strip(),
            scheme=scheme,
            host=host,
            port=port,
            ip="",
            pinned_url=str(url).strip(),
            host_header="",
            sni_hostname="",
            pin_active=False,
        )

    # ONE resolution: classified AND pinned from the same address set.
    import socket

    try:
        infos = socket.getaddrinfo(host, port)
    except OSError as e:
        raise ValueError(f"URL host '{host}' could not be resolved ({e})") from e
    ips: list[str] = []
    for info in infos:
        addr = str(info[4][0] or "").strip(
            "[]"
        )  # typeshed: getaddrinfo sockaddr[0] is str|int; host component is always str
        if addr and addr not in ips:
            ips.append(addr)
    if not ips:
        raise ValueError(f"URL host '{host}' resolved to no address")

    if not allowlisted and _INGEST_BLOCK_PRIVATE and _addresses_private(ips, allow_loopback=allow_loopback):
        _raise_private_host_error(host, allow_loopback)

    # Prefer an IPv4 answer when the host has both families: pods commonly
    # lack an IPv6 route, and the pin commits the fetch to ONE address (no
    # per-address fallback — falling back would re-open the rebinding window).
    ipv4 = next((a for a in ips if ":" not in a), "")
    ip = ipv4 or ips[0]

    host_for_url = f"[{ip}]" if ":" in ip else ip
    raw_netloc = parts.netloc
    userinfo, at, hostport = raw_netloc.rpartition("@")
    if hostport.startswith("["):
        closing = hostport.find("]")
        rest = hostport[closing + 1 :] if closing != -1 else ""
        new_hostport = f"{host_for_url}{rest}"
    else:
        _stem, colon, port_part = hostport.partition(":")
        new_hostport = host_for_url + colon + port_part
    new_netloc = (userinfo + at + new_hostport) if at else new_hostport
    pinned_url = urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
    default_port = _DEFAULT_PORTS[scheme]
    host_header = host if port == default_port else f"{host}:{port}"
    sni = host if scheme == "https" else ""
    return PinnedUrl(
        url=str(url).strip(),
        scheme=scheme,
        host=host,
        port=port,
        ip=ip,
        pinned_url=pinned_url,
        host_header=host_header,
        sni_hostname=sni,
        pin_active=True,
    )


def _raise_private_host_error(host: str, allow_loopback: bool) -> None:
    """Raise the same ValueError the plain policy checks emit (message
    parity for callers/tests)."""
    if allow_loopback:
        raise ValueError(
            f"URL host '{host}' resolves to a private/internal address "
            f"(INGEST_BLOCK_PRIVATE_HOSTS=true; add it to INGEST_ALLOW_HOSTS to permit)"
        )
    raise ValueError(f"URL host '{host}' resolves to a private/internal address (INGEST_BLOCK_PRIVATE_HOSTS=true)")
