"""The visitor's address behind Cloudflare (mib-infra#46, FR-REG-23, mib-web#17).

Behind Cloudflare the peer a service sees is Traefik, not the visitor. Cloudflare
puts the visitor's address in `CF-Connecting-IP`, the edge keeps it, and mib-web
forwards it on the calls it makes for a visitor.

**The header is believed from whoever sent it.** That is safe only because the
firewall admits Cloudflare's ranges and nothing else (NFR-42), and because
internal callers are trusted siblings; read it for an audit row or a per-IP
backstop, never as proof of identity.
"""
import ipaddress

from starlette.requests import HTTPConnection

VISITOR_IP_HEADER = "CF-Connecting-IP"


def visitor_ip(connection: HTTPConnection) -> str | None:
    """The visitor's address as a normalised string, or None.

    None when the header is absent or is not an IP address: the peer address is
    deliberately not a fallback, since in production that is the gateway, and
    recording it as the visitor is the defect this exists to remove. Callers
    store None as unknown (a nullable `inet` column accepts the result as is).
    """
    value = connection.headers.get(VISITOR_IP_HEADER)
    if value is None:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


__all__ = ["VISITOR_IP_HEADER", "visitor_ip"]
