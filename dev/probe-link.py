#!/usr/bin/env python3
# ============================================================================
# What does the redirect guard actually SEE when it probes a stream link?
#
#   python3 dev/probe-link.py 'http://panel/play/live.php?mac=..&stream=1&extension=ts&play_token=..'
#   python3 dev/probe-link.py --read 8192 '<url>'        # also taste the bytes
#
# Runs the real app.services.redirect_guard.link_is_alive() against a live URL
# through a recording transport, and prints every rung of its ladder - method,
# headers we sent, status or exception, elapsed ms - plus the verdict the guard
# would return. Use it when the stream log says
#
#   [Npo 1] redirect: fresh link dead (portal/MAC; HEAD ReadError) -> next candidate
#
# and the question is whether the origin is dead or merely PROBE-SHY: a live-TS
# endpoint that cannot answer a HEAD (or cannot honour a Range) hangs the
# connection up, which reads like death to a probe and plays fine in a player.
#
# --read N goes one step further and tastes the stream: N bytes of a plain GET
# (no Range, exactly what a player sends) are fetched and classified - MPEG-TS
# (0x47 sync byte every 188), a web page (the panel's error page: the token or
# the MAC was refused), or something else. That answers "is the link good?"
# instead of "does the probe like it?".
#
# GETTING A FRESH URL: the log masks play_token. The 302 does not - ask the app
# for the channel and read the Location header (add --insecure/-k equivalents as
# needed, and note the token is short-lived, so probe immediately):
#
#   curl -sI 'http://nas:8880/play/live/2.ts?u=USER&p=PASS' | grep -i '^location'
#
# No pytest, no database, no portal session: this script only needs httpx, so it
# also runs inside the built image on a NAS
# (docker exec -it stalker-proxy-manager python3 dev/probe-link.py ...).
# ============================================================================
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
import tempfile
import time

# importing app.services.redirect_guard pulls in app.portal.identity (and with
# it app.config, which logs its banner and creates the data dir): keep both out
# of the way of a diagnostic that must not touch the real installation
logging.disable(logging.CRITICAL)
os.environ.setdefault("SPM_DATA_DIR", tempfile.mkdtemp(prefix="spm-probe-link-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.portal.identity import STB_UA  # noqa: E402
from app.services.redirect_guard import (  # noqa: E402
    VALIDATE_TIMEOUT, _referer_of, link_is_alive,
)

TS_PACKET = 188
TS_SYNC = 0x47


class Recording(httpx.AsyncBaseTransport):
    """Delegates to a real transport and prints every rung as it happens."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        asked = "Range: " + request.headers["Range"] if "Range" in request.headers else "no Range"
        print(f"  {self.requests}. {request.method} {asked}")
        started = time.monotonic()
        try:
            response = await self._inner.handle_async_request(request)
        except Exception as exc:                      # noqa: BLE001 - report, re-raise
            ms = (time.monotonic() - started) * 1000
            print(f"     {ms:6.0f} ms  {type(exc).__name__}: {exc}")
            raise
        ms = (time.monotonic() - started) * 1000
        kind = response.headers.get("content-type", "?").split(";")[0]
        size = response.headers.get("content-length", "chunked/streaming")
        print(f"     {ms:6.0f} ms  HTTP {response.status_code}  {kind}  length={size}")
        for header in ("location", "content-range", "accept-ranges", "server"):
            if header in response.headers:
                print(f"                {header}: {response.headers[header]}")
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def classify(body: bytes) -> str:
    """What the origin actually served, in the words an operator needs."""
    if not body:
        return "NOTHING - the origin sent no bytes at all"
    head = body[:16].hex(" ")
    low = body[:4096].lower()
    if b"<html" in low or b"<!doctype" in low or low.lstrip().startswith(b"<?php"):
        return f"A WEB PAGE, not a stream ({head} ...) - the panel refused this request"
    if body[0] == TS_SYNC:
        aligned = sum(1 for i in range(0, len(body) - TS_PACKET + 1, TS_PACKET)
                      if body[i] == TS_SYNC)
        total = max(1, (len(body) - 1) // TS_PACKET)
        if aligned >= total * 0.8:
            return (f"MPEG-TS: 0x47 sync byte at {aligned}/{total} packet "
                    f"boundaries - this link serves the stream")
        return f"MPEG-TS-looking but misaligned ({aligned}/{total} sync bytes): {head} ..."
    if body[:4] == b"\x1aE\xdf\xa3":
        return "Matroska/WebM (EBML) - playable, but not the .ts the URL promised"
    if body[:3] == b"ID3" or body[:2] == b"\xff\xfb":
        return "audio (ID3/MPEG) - not a TV stream"
    return f"UNKNOWN bytes: {head} ..."


async def taste(url: str, nbytes: int, timeout: float, headers: dict) -> None:
    """One plain GET, N bytes read, connection closed. What a player does."""
    print(f"\n  tasting {nbytes} bytes of a plain GET (no Range):")
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers=headers) as client:
            async with client.stream("GET", url) as response:
                print(f"     HTTP {response.status_code}  "
                      f"{response.headers.get('content-type', '?').split(';')[0]}")
                body = b""
                async for chunk in response.aiter_bytes(4096):
                    body += chunk
                    if len(body) >= nbytes:
                        break
    except Exception as exc:                          # noqa: BLE001 - the point is to report it
        ms = (time.monotonic() - started) * 1000
        print(f"     {ms:6.0f} ms  {type(exc).__name__}: {exc}")
        print("     -> the origin would not serve bytes to a player either")
        return
    ms = (time.monotonic() - started) * 1000
    print(f"     {ms:6.0f} ms  read {len(body)} byte(s)")
    print(f"     -> {classify(body)}")


async def run(urls: list[str], args) -> int:
    worst = 0
    for url in urls:
        print(f"\n{url}")
        headers = {"User-Agent": args.ua, "Referer": _referer_of(url)}
        if args.no_referer:
            headers.pop("Referer")
        print(f"  User-Agent: {args.ua}")
        print(f"  Referer:    {headers.get('Referer', '(none)')}")
        print("  the guard's ladder:")
        transport = Recording()
        client = httpx.AsyncClient(transport=transport, timeout=args.timeout,
                                   follow_redirects=True, headers=headers)
        started = time.monotonic()
        try:
            result = await link_is_alive(url, timeout=args.timeout, client=client)
        finally:
            await client.aclose()
        ms = (time.monotonic() - started) * 1000
        verdict = "ALIVE" if result.alive else "DEAD"
        print(f"  verdict: {verdict} in {ms:.0f} ms, {transport.requests} request(s)"
              f" - trace: {result.detail}")
        if not result.alive:
            worst = 1
        if args.read:
            await taste(url, args.read, args.timeout, headers)
    return worst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe a stream link the way the redirect guard does, and say what "
                    "the origin really answered.",
        epilog="exit status: 0 = every URL alive, 1 = at least one vetoed by the guard")
    parser.add_argument("urls", nargs="+", metavar="URL", help="link(s) to probe")
    parser.add_argument("--timeout", type=float, default=VALIDATE_TIMEOUT,
                        help=f"per-request seconds (guard default {VALIDATE_TIMEOUT})")
    parser.add_argument("--read", type=int, default=0, metavar="N",
                        help="also fetch N bytes of a plain GET and classify them "
                             "(e.g. 8192 = ~43 TS packets)")
    parser.add_argument("--ua", default=STB_UA,
                        help="User-Agent to probe with (default: the STB identity the "
                             "guard uses)")
    parser.add_argument("--no-referer", action="store_true",
                        help="omit the origin-root Referer the guard sends")
    args = parser.parse_args(argv)
    for url in args.urls:
        if "://" not in url:
            parser.error(f"{url!r} is not a URL")
    try:
        return asyncio.run(run(args.urls, args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
