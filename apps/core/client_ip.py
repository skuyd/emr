"""Resolve the client at the first untrusted hop, never from an arbitrary header."""

import ipaddress

from django.conf import settings


def get_client_ip(request):
    raw_peer = request.META.get("REMOTE_ADDR", "")
    try:
        peer = ipaddress.ip_address(raw_peer)
    except ValueError:
        return ""
    trusted = tuple(ipaddress.ip_network(value) for value in getattr(settings, "TRUSTED_PROXY_NETWORKS", ()))
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if not forwarded or len(forwarded) > 4096:
        return peer.compressed
    hops = forwarded.split(",")
    if len(hops) > 32:
        return peer.compressed
    current = peer
    for raw_hop in reversed(hops):
        if not any(current in network for network in trusted):
            break
        try:
            if "%" in raw_hop:
                raise ValueError("Scoped proxy address is not supported")
            current = ipaddress.ip_address(raw_hop.strip())
        except ValueError:
            # A malformed trusted tail cannot establish a client boundary.
            return peer.compressed
    return current.compressed
