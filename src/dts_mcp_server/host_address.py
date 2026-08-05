"""Resolve the LAN address this gateway should be reached at.

The certificate SAN, the public base URL and the firewall scope all have to
agree on one address. Hardcoding it means every DHCP change silently breaks
TLS: the client rejects the certificate because the SAN no longer matches the
address it dialled. This module derives the address instead, so a re-bootstrap
picks up the new one.

Resolution order, first match wins:

1. ``DTS_MCP_SERVER_IP`` -- explicit override, always respected.
2. The interface the OS uses to reach the default gateway. Chosen over
   ``gethostname`` because a host with several adapters (VPN, Hyper-V, WSL,
   Docker) frequently resolves its own name to an address no client can route.
3. ``DTS_MCP_PUBLIC_BASE_URL``'s host, if it is a literal address.

A loopback, link-local or unspecified result is rejected: binding a
certificate to one of those produces a service only this machine can reach,
which fails at client-connect time rather than here.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

OVERRIDE_ENV = "DTS_MCP_SERVER_IP"
BASE_URL_ENV = "DTS_MCP_PUBLIC_BASE_URL"


class AddressResolutionError(RuntimeError):
    """No usable LAN address could be determined."""


def _usable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    )


def _probe_default_route() -> str | None:
    """Ask the OS which local address reaches the default gateway.

    Connecting a UDP socket assigns a local address from the routing table
    without sending a packet, so this works with no network round trip and no
    reachable peer at the far end.
    """
    for family, probe in (
        (socket.AF_INET, ("203.0.113.1", 9)),      # RFC 5737 documentation net
        (socket.AF_INET6, ("2001:db8::1", 9)),     # RFC 3849 documentation net
    ):
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            sock.connect(probe)
            candidate = sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
        try:
            if _usable(ipaddress.ip_address(candidate)):
                return candidate
        except ValueError:
            continue
    return None


def _from_base_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    host = urlsplit(base_url).hostname
    if not host:
        return None
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None  # a DNS name needs no IP SAN
    return host if _usable(parsed) else None


def resolve_server_ip(environ: dict[str, str] | None = None) -> str:
    """Return the address to publish, or raise AddressResolutionError."""
    env = os.environ if environ is None else environ

    override = (env.get(OVERRIDE_ENV) or "").strip()
    if override:
        try:
            parsed = ipaddress.ip_address(override)
        except ValueError as error:
            raise AddressResolutionError(
                f"{OVERRIDE_ENV}={override!r} is not a valid IP address."
            ) from error
        if not _usable(parsed):
            raise AddressResolutionError(
                f"{OVERRIDE_ENV}={override!r} is a loopback, link-local or "
                "otherwise unroutable address; remote clients could not reach "
                "the gateway at it."
            )
        return override

    detected = _probe_default_route()
    if detected:
        return detected

    from_url = _from_base_url(env.get(BASE_URL_ENV))
    if from_url:
        return from_url

    raise AddressResolutionError(
        "Could not determine this host's LAN address. Connect the network "
        f"adapter, or set {OVERRIDE_ENV} to the address clients will dial."
    )


def resolve_ipv4_network(server_ip: str, prefix: int = 24) -> str:
    """Return the CIDR block to scope the inbound firewall rule to.

    IPv6 is not narrowed to a prefix here: a temporary privacy address would
    make the rule wrong as soon as it rotates, so callers should scope IPv6 by
    profile instead.
    """
    address = ipaddress.ip_address(server_ip)
    if not isinstance(address, ipaddress.IPv4Address):
        raise AddressResolutionError(
            f"{server_ip} is not IPv4; scope the firewall rule manually."
        )
    return str(ipaddress.ip_network(f"{server_ip}/{prefix}", strict=False))
