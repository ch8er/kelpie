"""One loopback check for every listener and for the resources and traces settings
(#248 D12).

Whether an address is loopback decides what Kelpie exposes without a token: a listener
bound there takes spans or serves the farm view only to its own host, and a URL there may
carry a bearer token over plain http. :func:`is_loopback` is the single implementation;
its callers differ only in what they hand it:

- the listeners' connection guards (:mod:`kelpie.httpguard`) normalize a bind address
  first (spaces, case, brackets, a trailing dot) and count IPv4-mapped loopback
  (``::ffff:127.0.0.1``) too (``mapped=True``);
- the traces and resources settings check a bind host or a URL's host name as given;
- ``[agent] relay`` takes IP literals only (``names=False, brackets=False``): its config
  takes the brackets off and maps ``localhost`` to ``127.0.0.1`` itself.

Out of scope: the plain-http URL checks of the HTTP helper (``kelpie.net``, which must
stand alone), the GitHub connector and Telegram; they are not listeners.

Pure: no name resolution, no I/O.
"""

from __future__ import annotations

import ipaddress

__all__ = ["is_loopback"]


def is_loopback(
    host: str, *, names: bool = True, mapped: bool = False, brackets: bool = True
) -> bool:
    """Whether ``host`` is loopback: the name ``localhost`` (any case, surrounding spaces
    ignored; only with ``names``) or a loopback IP literal (brackets around it ignored;
    without ``brackets`` a bracket anywhere makes it no literal). ``mapped``: an
    IPv4-mapped IPv6 literal counts by its IPv4 address. Any other name is not loopback:
    nothing is resolved."""
    if not isinstance(host, str):  # typed str; never raise on anything else
        return False
    if names and host.strip().lower() == "localhost":
        return True
    if not brackets and ("[" in host or "]" in host):
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if mapped and isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback
