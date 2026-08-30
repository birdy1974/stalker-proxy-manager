"""
Built-in mock Stalker portal for development, CI and first-time testing.

Why this exists: real Stalker portals usually block datacenter IPs (verified in
Phase 1 - the test portal dropped every request from the dev sandbox), so the
whole application must be testable without one.

Usage: add a portal in the GUI with URL  http://<host>:8880/mock/c/  and one of
the demo MACs listed in MOCK_MACS. The mock emulates:
  * /mock/c/xpcom.common.js  (so resolver strategy 1 is exercised)
  * /mock/c/portal.php       handshake / profile / account_info / genres /
                             ordered lists (itv, vod, series) / seasons /
                             episodes / create_link
  * /mock/ts/<file>.ts       infinite MPEG-TS testsrc stream (via ffmpeg)
  * behaviours: per-MAC concurrency limit (default 1) so the fallback logic is
    really triggered, plus /mock/_control toggles: offline, slow, busy, and
    the three that reproduce how real panels *refuse*:
      create_link_error  answer create_link with HTTP 200 + {"js":{"error":X}}
      token_rejects=N    the next N calls reject the bearer with a 200
                         {"js":{"error":"token"}} - exactly how Ministra
                         expires a session - then behave again
      mac_placeholder    hand out links containing %mac% instead of a MAC

The dataset is deterministic: 3 live genres x 4 channels, 2 vod genres x 12
movies, 2 series genres x 6 series (2-3 seasons x 5 episodes).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from ..config import FFMPEG_BIN

log = logging.getLogger("spm.mockportal")
router = APIRouter(tags=["mock-portal"])

# ---------------------------------------------------------------------------
# Demo dataset (deterministic - no randomness between restarts)
# ---------------------------------------------------------------------------
MOCK_MACS = {
    "00:1A:79:AA:AA:01": {"phone": "2032-12-31 00:00:00"},
    "00:1A:79:AA:AA:02": {"phone": "2032-12-31 00:00:00"},
    "00:1A:79:BB:BB:01": {"phone": "2024-01-01 00:00:00"},   # expired on purpose
}

LIVE_GENRES = {"1": "News", "2": "Sport", "3": "Kids", "4": "Movies & Series",
               "5": "Documentary", "6": "Music", "7": "Regional",
               "8": "Entertainment", "9": "International", "10": "Lifestyle"}
_LIVE_NAMES = {
    "1": ["NPO 1", "NPO 2", "RTL Nieuws", "BBC News"],
    "2": ["ESPN", "Ziggo Sport", "Eurosport 1", "Sky Sports"],
    "3": ["Cartoon Network", "Nickelodeon", "Baby TV", "Boomerang"],
    # generated extra channels so lists/filters/pagination are visibly exercised
    "4": [f"Cinema {n}" for n in ("One", "Two", "HD", "Extra")],
    "5": [f"Docu {n}" for n in ("World", "Wild", "History", "24")],
    "6": [f"Music {n}" for n in ("Hits", "Classic", "Live", "TV")],
    "7": [f"Region {n}" for n in ("NH", "Brabant", "Limburg", "Randstad")],
    "8": [f"Fun {n}" for n in ("TV", "Prime", "XL", "Late")],
    "9": [f"World {n}" for n in ("Europe", "Asia", "US", "Global")],
    "10": [f"Life {n}" for n in ("Style", "Home", "Travel", "Food")],
}
VOD_GENRES = {"11": "Action", "12": "Comedy", "13": "Thriller", "14": "SciFi",
              "15": "Family", "16": "Drama"}
_VOD_NAMES = {
    "11": ["Mock Runner 2049", "Fast & Curious", "The Cache", "Proxy Hard", "Stream Club",
           "比特率 故事", "Null Island", "The Fallback", "Data Moon", "Kernel Panic",
           "Terminal Velocity", "Signal Lost"],
    "12": ["Funny Packets", "The Jitter", "Latency Blues", "Buffer Day", "Ping Pong",
           "Two Tickets", "LOL Sprintf", "Comedy of Errors", "The Reboot", "Macro Hard",
           "Patch Adams Family", "Click Wars"],
    "13": [f"The {n}" for n in ("Silent Handshake", "Fourth Mac", "Dead Timeout",
                                  "Retry Loop", "Cold Proxy", "Null Session")],
    "14": [f"{n}" for n in ("Star Packets", "Galactic Buffer", "The Uplink",
                            "Probe", "Encoded", "Bitstream")],
    "15": [f"{n}" for n in ("Family Bandwidth", "The Little Stream", "Packets for Kids",
                            "Movie Night", "Grandma's Playlist", "Story Time TV")],
    "16": [f"{n}" for n in ("The Encoder's Wife", "Last Token", "Rain on the Dish",
                            "The Long Reconnect", "Broken Chain", "A Quiet Stream")],
}
SERIE_GENRES = {"21": "SciFi", "22": "Drama", "23": "Comedy", "24": "Crime",
                "25": "Documentary"}
_SERIE_NAMES = {
    "21": ["Portal Quest", "The Token", "Bandwidth", "Handshakers", "Streamfall", "M3U Cubs"],
    "22": ["The Resolver", "Silent Packets", "Ashes of GPIO", "NVR Diaries", "Episodes", "Fallout LAN"],
    "23": [f"{n}" for n in ("Buffer Brothers", "The Latency Show", "Office Streams",
                            "Uploads", "Bit Jokes", "Frame Drops")],
    "24": [f"{n}" for n in ("MAC Address", "The Proxy Murders", "Cold Stream",
                            "Evidence Packets", "Wireshark Unit", "The Sniffer")],
    "25": [f"{n}" for n in ("How Streams Work", "Planet Fiber", "The Codec Story",
                            "Networks of Europe", "Inside the CDN", "Digital Rivers")],
}

PAGE_SIZE = 14  # exactly what real portals use

# runtime state: mac -> in-flight stream count, plus behaviour toggles.
#   create_link_error  portal-level refusal code answered with HTTP 200 on
#                      create_link ("limit", "nothing_to_play", "link_fault",
#                      "access_denied") - the shape real panels use and the one
#                      that used to reach the log as "no usable url"
#   token_rejects      number of following non-handshake calls that reject the
#                      bearer with a 200 {"js":{"error":"token"}}
#   mac_placeholder    hand out `%mac%` inside the link instead of a real MAC
_STATE = {"usage": {}, "offline": False, "slow": False, "max_per_mac": 1, "note": "",
          "create_link_error": "", "token_rejects": 0, "mac_placeholder": False}


def _usage(mac: str) -> int:
    return _STATE["usage"].get(mac, 0)


def _js(payload) -> JSONResponse:  # Stalker envelope: {"js": ...}
    return JSONResponse({"js": payload})


def _paged(items: list, page: int) -> dict:
    start = PAGE_SIZE * (page - 1)
    return {"data": items[start:start + PAGE_SIZE], "total_items": len(items),
            "max_page_items": PAGE_SIZE}


def _live_rows():
    rows = []
    ch = 1
    for gid, names in _LIVE_NAMES.items():
        for n in names:
            rows.append({
                "id": str(1000 + ch), "name": n, "number": str(ch),
                "cmd": f"ffmpeg http://mock/ts/{1000 + ch}.ts",
                "logo": "", "tv_genre_id": gid, "tv_archive": 0, "censored": 0,
                "use_http_tmp_link": 1, "status": 1,
            })
            ch += 1
    return rows


def _vod_rows():
    rows = []
    vid = 1
    for gid, names in _VOD_NAMES.items():
        for idx, n in enumerate(names):
            rows.append({
                "id": str(2000 + vid), "name": n, "o_name": n,
                "cmd": f"ffmpeg http://mock/vod/{2000 + vid}.mp4",
                "screenshot_uri": "", "year": str(2010 + (vid % 15)),
                "description": f"Mock description for {n}.",
                "director": "A. Uthor", "actors": "Mock Actor, Demo Actress",
                "rating_imdb": str(round(5 + (vid % 5) * 0.9, 1)),
                "time": str(90 + (vid % 6) * 10), "category_id": gid,
                "category_name": VOD_GENRES[gid], "is_series": 0,
                "added": f"2024-{(vid % 12) + 1:02d}-01", "position": vid,
            })
            vid += 1
    return rows


def _series_rows():
    rows = []
    sid = 1
    for gid, names in _SERIE_NAMES.items():
        for n in names:
            rows.append({
                "id": str(3000 + sid), "name": n, "o_name": n,
                "cmd": "",                      # series have EMPTY cmd (container!)
                "is_series": 1, "series": [],
                "category_id": gid, "category_name": SERIE_GENRES[gid],
                "screenshot_uri": "", "year": str(2015 + (sid % 8)),
                "rating_kinopoisk": str(round(6 + (sid % 4) * 0.8, 1)),
            })
            sid += 1
    return rows


def _seasons_of(series_id: str) -> list[dict]:
    sid = int(series_id) - 3000
    count = 2 + (sid % 2)                        # 2 or 3 seasons, deterministic
    return [{
        "id": f"{3000 + sid}:{sn}", "series_id": series_id, "season_id": str(sn),
        "name": f"Season {sn}", "is_season": 1, "cmd": "", "series": [],
    } for sn in range(1, count + 1)]


def _episodes_of(series_id: str, season_id: str | None) -> list[dict]:
    sid = int(series_id) - 3000
    seasons = [s["season_id"] for s in _seasons_of(series_id)]
    out = []
    for sn in ([season_id] if season_id else seasons):
        for ep in range(1, 6):                   # 5 episodes per season
            eid = 4000 + sid * 100 + int(sn) * 10 + ep
            out.append({
                "id": str(eid), "name": f"Episode {ep}",
                "cmd": f"ffmpeg http://mock/vod/{eid}.mp4",
                "series": [int(sn), ep], "season_id": sn,
                "is_series": 0, "time": "42", "screenshot_uri": "",
            })
    return out


# ---------------------------------------------------------------------------
# xpcom.common.js (resolver strategy 1) - mirrors a real portal file's shape
# ---------------------------------------------------------------------------
@router.get("/mock/c/xpcom.common.js", response_class=PlainTextResponse)
async def xpcom(request: Request):
    # Shape compatible with resolver._parse_xpcom(): the var-pattern regex over
    # this file's own URL yields the fragments that `ajax_loader` reassembles.
    return (
        "var pattern = /(http:\\/\\/[^\\/]+)\\/(.*)\\/([^\\/]+\\.js)/;\n"
        "this.portal_protocol = pattern.exec(document.location.href)[1];\n"
        "this.portal_ip = pattern.exec(document.location.href)[2];\n"
        "this.portal_path = pattern.exec(document.location.href)[3];\n"
        "this.ajax_loader = this.portal_protocol + '/' + this.portal_ip + '/portal.php';\n"
    )


async def _guard(request: Request):
    """Common behaviour toggles: offline + latency simulation."""
    if _STATE["offline"]:
        return PlainTextResponse("offline (mock control)", status_code=503)
    if _STATE["slow"]:
        await asyncio.sleep(3)
    return None


def _portal_mac(request: Request) -> str:
    """Real STBs send the mac as cookie or as query param - accept both."""
    return request.cookies.get("mac", "") or request.query_params.get("mac", "") or ""


def _auth(request: Request) -> tuple[str, JSONResponse | None]:
    """Post-handshake authorization: known mac + the bearer token that the
    handshake issued. The handshake itself is permissive (see portal_php).

    `token_rejects` emulates the way Ministra actually expires a session: NOT
    with a 401 but with HTTP 200 + {"js":{"error":"token"}}, which a lenient
    client reads as "empty result". A client that does not re-handshake on it
    stalls until its token TTL runs out - which is exactly the bug this toggle
    guards against.
    """
    mac = _portal_mac(request)
    if mac not in MOCK_MACS:
        return mac, JSONResponse({"js": {"error": "unknown mac"}}, status_code=403)
    if _STATE.get("token_rejects", 0):
        _STATE["token_rejects"] = int(_STATE["token_rejects"]) - 1
        log.warning("mock portal: rejecting bearer for %s with HTTP 200 token error "
                    "(%d left)", mac, _STATE["token_rejects"])
        return mac, JSONResponse({"js": {"error": "token",
                                         "msg": "timeout of authorization token"}},
                                 status_code=200)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer mock-" + mac.replace(":", "")):
        return mac, JSONResponse({"js": {"error": "token"}}, status_code=401)
    return mac, None


# ---------------------------------------------------------------------------
# The portal itself
# ---------------------------------------------------------------------------
@router.api_route("/mock/c/portal.php", methods=["GET"])
async def portal_php(request: Request):  # noqa: A002
    # type/action are read straight from the query string (no FastAPI schema
    # validation): portals/emulators send arbitrary extra params and the mock
    # must never answer 422 for the canonical handshake shape.
    qp = request.query_params
    type = qp.get("type", "")  # noqa: A002
    action = qp.get("action", "")

    guard = await _guard(request)
    if guard is not None:
        return guard
    mac = _portal_mac(request)

    # Handshake is deliberately permissive, like real portals: ANY mac gets a
    # token (the known-MAC allowlist + bearer check apply to the calls after
    # the handshake). This is the canonical STB/smoke-test flow:
    #   ?type=stb&action=handshake&mac=00:1A:79:AA:AA:01
    # We also accept type=stb with a missing/extra action (some emulators drop
    # or rename params), so a handshake is never rejected by param shape.
    if type == "stb" and (action == "handshake" or not action):
        token = "mock-" + (mac.replace(":", "") if mac else "000000000000")
        log.info("mock portal: query=%s mac=%s -> handshake token issued",
                 request.url.query or "-", mac or "-")
        return _js({"token": token})

    mac, err = _auth(request)
    if err is not None:
        # log the resolution so `docker logs` shows WHY a call was rejected
        # (unknown mac / missing token) instead of the client guessing.
        log.warning("mock portal: type=%s action=%s mac=%s -> HTTP %d",
                    type, action, mac or "-", err.status_code)
        return err
    log.debug("mock portal: type=%s action=%s mac=%s -> ok", type, action, mac or "-")
    if type == "stb" and action == "get_profile":
        return _js({"mac": mac, "locale": "en_GB.utf8", "hd": 1, "ver": "MockPortal 1.0"})
    if type == "account_info" and action == "get_main_info":
        return _js({"mac": mac, "phone": MOCK_MACS[mac]["phone"], "fname": "Mock User"})

    if type == "itv" and action == "get_genres":
        return _js([{"id": gid, "title": name, "number": i * 10, "censored": 0}
                    for i, (gid, name) in enumerate(LIVE_GENRES.items(), 1)])
    if type == "itv" and action == "get_ordered_list":
        genre = qp.get("genre")
        page = int(qp.get("p", "1") or "1")
        rows = _live_rows()
        if genre:
            rows = [r for r in rows if r["tv_genre_id"] == genre]
        return _js(_paged(rows, page))
    if type == "itv" and action == "get_all_channels":
        return _js({"data": _live_rows()})

    if type == "vod" and action == "get_categories":
        return _js([{"id": gid, "title": n, "alias": ""} for gid, n in VOD_GENRES.items()])
    if type == "series" and action == "get_categories":
        return _js([{"id": gid, "title": n, "alias": ""} for gid, n in SERIE_GENRES.items()])

    if type in ("vod", "series") and action == "get_ordered_list":
        movie_id = qp.get("movie_id")
        if movie_id:  # series drill-down: seasons or episodes
            season_id = qp.get("season_id")
            if season_id:
                return _js({"data": _episodes_of(movie_id, season_id),
                            "total_items": 5, "max_page_items": 50})
            seasons = _seasons_of(movie_id)
            eps = _episodes_of(movie_id, None) if not seasons else []
            return _js({"data": seasons or eps, "total_items": len(seasons) or len(eps),
                        "max_page_items": 50})
        page = int(qp.get("p", "1") or "1")
        category = qp.get("category")
        if type == "series":
            rows = _series_rows()
            if category:
                rows = [r for r in rows if r["category_id"] == category]
            return _js(_paged(rows, page))
        rows = _vod_rows()
        if category:
            rows = [r for r in rows if r["category_id"] == category]
        return _js(_paged(rows, page))

    if action == "create_link":
        cmd = qp.get("cmd", "")
        # A portal-level refusal with HTTP 200 (real panels answer exactly
        # like this, and the code is the only thing that tells "this MAC is
        # busy" from "this channel is gone").
        if _STATE.get("create_link_error"):
            code = str(_STATE["create_link_error"])
            log.warning("mock portal: create_link refused for %s with 200 + error=%r",
                        mac, code)
            return _js({"error": code})
        # busy-MAC emulation: refuse when this MAC already streams something
        if _usage(mac) >= _STATE["max_per_mac"]:
            return JSONResponse({"js": {"error": "account is in use"}}, status_code=403)
        if ".ts" in cmd or ".mp4" in cmd or ".m3u8" in cmd:
            base = str(request.base_url).rstrip("/") + "/mock/"
            if _STATE.get("mac_placeholder"):
                # one template for every box: the STB has to fill in its own MAC
                return _js({"cmd": f"ffmpeg {base}ts/%mac%.ts?m=%MAC%"})
            url = cmd.split()[-1].replace("http://mock/", base)
            return _js({"cmd": "ffmpeg " + url})
        return JSONResponse({"js": {"error": "bad cmd"}}, status_code=404)

    if type == "itv" and action == "get_short_epg":
        ch = qp.get("ch_id", "")
        now = int(time.time())
        return _js([{"id": 1, "ch_id": ch, "name": "Mock programme", "descr": "Demo EPG entry",
                     "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)),
                     "time_to": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now + 3600)),
                     "duration": "01:00:00"}])

    return JSONResponse({"js": {"error": f"unknown mock call {type}/{action}"}}, status_code=404)


@router.get("/mock/epg.xml")
async def mock_epg_xml():
    """Tiny XMLTV demo source so Phase-3 EPG ingest/match/output is verifiable
    end-to-end against the built-in mock portal (channels + 6h of programmes)."""
    from xml.sax.saxutils import escape as _e

    names = [n for names in _LIVE_NAMES.values() for n in names]
    now = int(time.time() // 3600 * 3600)          # align to full hour
    L = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="mock-portal">']
    for n in names:
        cid = _e(n.lower().replace(" ", "-"))
        L.append(f'  <channel id="{cid}"><display-name>{_e(n)}</display-name></channel>')
    for n in names:
        cid = _e(n.lower().replace(" ", "-"))
        for h in range(-1, 6):
            st, en = now + h * 3600, now + (h + 1) * 3600
            fmt = time.strftime
            L.append(
                f'  <programme start="{fmt("%Y%m%d%H%M%S", time.gmtime(st))} +0000" '
                f'stop="{fmt("%Y%m%d%H%M%S", time.gmtime(en))} +0000" channel="{cid}">'
                f'<title>{_e(n)} demo show {h if h >= 0 else "prev"}</title>'
                f'<desc>Automatically generated mock programme for {_e(n)}.</desc>'
                f'<category>{_e("Demo")}</category></programme>')
    L.append("</tv>")
    return Response("\n".join(L) + "\n", media_type="application/xml")


# ---------------------------------------------------------------------------
# Demo media generator: infinite testsrc stream (any ffmpeg binary).
# Container = fragmented MP4: ffmpeg probes the container from BYTES, so the
# .ts/.mp4 URL names don't matter; fMP4 streams forever (Stalker VOD links are
# usually plain files too). We avoid MPEG-TS *input* here because some static
# ffmpeg builds have a broken TS demuxer on exotic CPUs (seen in CI) - the
# proxy pipeline must stay verifiable everywhere. TS output to clients is fine.
# ---------------------------------------------------------------------------
async def _ts_generator(kind: str):
    """Spawn ffmpeg testsrc -> stdout (fMP4)."""
    args = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
        "-re",
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-g", "25", "-b:v", "800k", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


@router.get("/mock/ts/{name}.ts")
async def mock_ts(request: Request, name: str):
    mac = request.cookies.get("mac", "") or request.query_params.get("mac", "")
    if mac:
        _STATE["usage"][mac] = _usage(mac) + 1

    async def gen():
        try:
            async for chunk in _ts_generator(name):
                yield chunk
        finally:
            if mac:
                _STATE["usage"][mac] = max(0, _usage(mac) - 1)

    # octet-stream on purpose: real proxies/players sniff the payload anyway
    return StreamingResponse(gen(), media_type="application/octet-stream")


@router.get("/mock/vod/{name}.mp4")
async def mock_vod(name: str):
    return StreamingResponse(_ts_generator(name), media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# Control endpoint for demos/tests (flip offline/slow/busy on the fly)
# ---------------------------------------------------------------------------
_TOGGLE_KEYS = ("offline", "slow", "max_per_mac", "create_link_error",
                "token_rejects", "mac_placeholder")


@router.post("/mock/_control")
async def control(payload: dict):
    for k in _TOGGLE_KEYS:
        if k in payload:
            _STATE[k] = payload[k]
    log.warning("mock portal control: %s", {k: _STATE[k] for k in _TOGGLE_KEYS})
    return {"state": {k: _STATE[k] for k in _TOGGLE_KEYS + ("note",)}}


@router.get("/mock/_state")
async def state():
    return {"state": {**_STATE, "usage": dict(_STATE["usage"])}}
