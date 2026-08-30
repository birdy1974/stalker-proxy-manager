"""Shared outbound HTTP client: one trust policy for the whole app.

Some deployments (mitm-proxied LANs/NAS containers, and this dev sandbox) ship
a broken/ancient certifi bundle while the OS trust store is correct. Creating
our own SSL context makes httpx use the OS store (same behaviour as urllib),
with TLS verification left fully enabled.

Verification is ON for every outbound call - including the Stalker portals
themselves, which is where it used to differ: `app/portal/client.py` built its
own `httpx.AsyncClient` and therefore used certifi, while the EPG/logo fetchers
went through here. A panel with a self-signed or incomplete chain then failed
TLS *only* on the portal calls, which is the one path a user cannot work around
from the GUI. Both now share this helper, and a panel with a genuinely broken
chain gets an explicit, per-portal opt-out instead of a global `verify=False`:

    Portal.tls_insecure  (GUI -> Portals -> edit -> "Allow broken TLS")

Never make that the default, and never apply it to our own credential or
output paths - it disables the only thing standing between a portal URL and a
man-in-the-middle.
"""
from __future__ import annotations

import ssl

import httpx

_CTX = ssl.create_default_context()


def outbound_client(*, insecure: bool = False, **kwargs) -> httpx.AsyncClient:
    """`httpx.AsyncClient` with the OS trust store (or explicitly none).

    `insecure=True` skips certificate verification *for this client only*. It
    exists so one panel with a broken chain does not force a code change; every
    caller derives it from a stored, per-portal flag that defaults to False.
    """
    if insecure:
        kwargs["verify"] = False
    else:
        kwargs.setdefault("verify", _CTX)
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(**kwargs)
