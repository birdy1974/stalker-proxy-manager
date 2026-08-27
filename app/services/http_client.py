"""Shared outbound HTTP client with system-CA trust.

Some deployments (mitm-proxied LANs/NAS containers, and this dev sandbox) ship
a broken/ancient certifi bundle while the OS trust store is correct. Creating
our own SSL context makes httpx use the OS store (same behaviour as urllib),
with TLS verification left fully enabled. Never set verify=False.
"""
from __future__ import annotations

import ssl

import httpx

_CTX = ssl.create_default_context()


def outbound_client(**kwargs) -> httpx.AsyncClient:
    kwargs.setdefault("verify", _CTX)
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(**kwargs)
