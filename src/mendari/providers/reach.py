# SPDX-License-Identifier: Apache-2.0
"""Where an endpoint a person set in the console may lead (ADR-0021).

A deployment's own endpoints come from its environment (`MENDARI_AGENT_URL`, `LLM_BASE_URL`,
`JEV_API_URL`): whoever sets them runs the server, and may point it at a service on its own
network. An agent's connection is set by an organization's administrator through the web
(`core/connections.py`), so the server must not become a way to reach what only the server can
reach: this machine, its private network, a cloud's metadata address.

Two rules, and which applies is the deployment's choice:

- **With `MENDARI_PROVIDER_ALLOWED_ORIGINS` set**, an endpoint's origin is on that list or it is
  refused (`providers/http.py` `require_allowed`). The list is the operator's statement of what
  the server may call, so an origin on it may be anywhere, the site's own network included, and
  is called through the deployment's shared client (which follows its proxy settings).
- **Without a list**, an endpoint leads to the public internet only (`check_public`): its host
  is not this machine, a single-label name or a number that is not a plain IP address
  (`name_problem`), and every address it resolves to is a public one (not loopback, private,
  link-local, shared, multicast or reserved, nor an IPv4 address of those kinds wrapped in
  IPv6). It is called through `public_client`, whose every connection is
  opened to an address that passed that check at the moment of connecting (`PublicOnly`), with
  no proxy in between: a name that answers with a public address when it is set and a private
  one when it is called connects to nothing. TLS is still checked against the name.

Either way: https (plain http to this machine only, which the second rule then refuses),
no redirect is followed, and an answer is read as the agent contract or not at all
(`providers/http.py`).
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from mendari.core.https_only import is_local_host
from mendari.errors import ProviderError

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
# The NAT64 well-known prefix (RFC 6052): its last 32 bits are an IPv4 address.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
# 6to4 (RFC 3056): bits 16 to 48 are an IPv4 address.
_6TO4 = ipaddress.IPv6Network("2002::/16")
HOW_TO_ALLOW = (
    "a connection set here leads to the public internet only; for a service on your own "
    "network, whoever runs this deployment lists its origin in MENDARI_PROVIDER_ALLOWED_ORIGINS"
)


class NotPublic(Exception):
    """A host that is not on the public internet, or that cannot be looked up: `why` says
    which, in words a person reads; never an address a name resolved to."""

    def __init__(self, why: str):
        self.why = why
        super().__init__(why)


def _literal(host: str) -> Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _embedded(address: Address) -> Address:
    """The IPv4 address an IPv6 address stands for, if it wraps one (IPv4-mapped, NAT64, 6to4):
    a private IPv4 address is as private in any of those forms."""
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address in _6TO4:
        return ipaddress.IPv4Address((int(address) >> 80) & 0xFFFFFFFF)
    return address


def is_public(address: Address) -> bool:
    """Whether an address is one on the public internet: global and not multicast, read as the
    IPv4 address it stands for when it wraps one."""
    candidate = _embedded(address)
    return candidate.is_global and not (
        candidate.is_multicast
        or candidate.is_unspecified
        or candidate.is_reserved
        or candidate.is_link_local
        or candidate.is_loopback
        # fec0::/10, site-local addresses, retired but still routed inside a site.
        or (isinstance(candidate, ipaddress.IPv6Address) and candidate.is_site_local)
    )


def name_problem(host: str) -> str | None:
    """Why a host is refused without looking it up, or None: this machine, a single-label name
    (a service on the server's own network), an address that is not public, or a number that
    is not an address in its plain form (`127.1`, `0x7f.0.0.1`, `0177.0.0.1`, `2130706433`:
    resolvers read those as addresses, and not all the same way, so none is looked up)."""
    literal = _literal(host)
    if literal is not None:
        return None if is_public(literal) else "its address is not one on the public internet"
    if is_local_host(host):
        return "it is this machine"
    labels = host.rstrip(".").split(".")
    if labels[-1].isdigit() or labels[-1].lower().startswith("0x"):
        return "its address is a number that is not written as a plain IP address"
    if len(labels) < 2:
        return "a name without a dot is one on this server's own network"
    return None


def _lookup(host: str, port: int) -> list[str]:
    """Every address `host` resolves to (the system's resolver)."""
    found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(item[4][0]) for item in found))


# What looks a name up: the system's resolver; a test gives its own.
resolve = _lookup


def public_addresses(host: str, port: int) -> list[str]:
    """The addresses to connect to for `host`: every one it resolves to, all of them public, or
    `NotPublic`. A name that resolves to a public address and a private one is refused whole: a
    connection could go to either."""
    problem = name_problem(host)
    if problem is not None:
        raise NotPublic(problem)
    literal = _literal(host)
    if literal is not None:
        return [str(literal)]
    try:
        found = resolve(host, port)
    except (OSError, UnicodeError):
        raise NotPublic("its name could not be looked up from this server") from None
    addresses = [address for address in map(_literal, found) if address is not None]
    if not addresses:
        raise NotPublic("its name could not be looked up from this server")
    if not all(is_public(address) for address in addresses):
        raise NotPublic("its name leads to an address that is not on the public internet")
    return [str(address) for address in addresses]


def _host_and_port(url: str) -> tuple[str, int]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ProviderError("invalid_endpoint", "Agent endpoint is not a valid URL") from None
    host = parts.hostname or ""
    return host, port or (443 if parts.scheme == "https" else 80)


def check_name(url: str) -> None:
    """`invalid_endpoint` when `url`'s host is refused without a look-up (`name_problem`):
    checked before every call to a connection set in the console, where the look-up itself is
    the connection's (`PublicOnly`)."""
    host, _ = _host_and_port(url)
    problem = name_problem(host)
    if problem is not None:
        raise ProviderError("invalid_endpoint", f"{problem}: {HOW_TO_ALLOW}")


def check_public(url: str) -> None:
    """`invalid_endpoint` unless `url` leads to the public internet now (`public_addresses`):
    checked when a connection is set and when it is checked on request."""
    host, port = _host_and_port(url)
    try:
        public_addresses(host, port)
    except NotPublic as error:
        raise ProviderError("invalid_endpoint", f"{error.why}: {HOW_TO_ALLOW}") from None


class PublicOnly(httpcore.SyncBackend):
    """A network backend that opens a connection only to a public address: it looks the host up
    itself, refuses unless every address is public, and connects to the address it checked,
    never to the name again. TLS is negotiated above this, for the name."""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        try:
            addresses = public_addresses(host, port)
        except NotPublic as error:
            raise httpcore.ConnectError(error.why) from None
        failed: Exception | None = None
        for address in addresses:
            try:
                return super().connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                failed = error
        assert failed is not None  # `addresses` is never empty
        raise failed


class _PublicTransport(httpx.HTTPTransport):
    """httpx's transport over a pool that connects through `PublicOnly`, and never through a
    proxy: the check is of the address the connection is opened to."""

    def __init__(self) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=32,
            max_keepalive_connections=16,
            keepalive_expiry=5.0,
            network_backend=PublicOnly(),
        )


_LOCK = threading.Lock()
_CLIENT: httpx.Client | None = None


def public_client() -> httpx.Client:
    """The one client connections set in the console are called through where the deployment
    lists no origins: no redirect, no proxy, public addresses only (`PublicOnly`)."""
    global _CLIENT
    with _LOCK:
        if _CLIENT is None or _CLIENT.is_closed:
            _CLIENT = httpx.Client(
                transport=_PublicTransport(), follow_redirects=False, trust_env=False
            )
        return _CLIENT
