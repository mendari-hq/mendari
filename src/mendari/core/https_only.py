# SPDX-License-Identifier: Apache-2.0
"""Which addresses may be reached without TLS: this machine, and nothing else.

A registry token, an agent archive and a recall list cross the network. Over plain http anyone on
the path reads the token and swaps the answer, so a registry is reached over https. A registry on
this machine (a developer's, a test's, the docker sandbox's published port) has no certificate,
and its traffic never leaves the machine, so http is allowed there.
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit


def is_local_host(host: str | None) -> bool:
    """This machine: `localhost`, a `.localhost` name or a loopback address."""
    name = (host or "").strip("[]").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ip_address(name).is_loopback
    except ValueError:
        return False


def is_https_or_local(url: str) -> bool:
    """Whether `url` is https with a host, or http to this machine. A URL that carries a user
    or a password is neither: credentials do not belong in an address."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if not host or parts.username is not None or parts.password is not None:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and is_local_host(host))
