"""
"What is on now", from the portal itself - with the discipline that keeps it cheap (R9).

`get_short_epg` is one request per channel, so the *only* reason this feature
exists rather than being a loop in a router is the accounting around it. EStalker's
rules are copied deliberately (`getshortepg.py`, `live.py:2129`):

* **only rows someone is looking at.** The endpoint takes the ids the page actually
  rendered, and caps the batch. Walking a whole catalogue with one request per
  channel is exactly the pattern that gets an IP banned, and a guide tooltip is not
  worth a blocked box.
* **deduped per batch**, so a table that renders the same source twice asks once.
* **XMLTV wins when it exists.** A configured guide source is free to read and
  covers every channel at once; the portal call is the fallback for channels with no
  guide, which on a normal install is nobody - the whole point of the ordering.
* **a refusal stops the batch for that portal**, not just that row: after
  `MAX_PORTAL_FAILURES` answered-wrong requests the rest of that portal's channels
  are skipped with a reason, because a panel that 403s or 503s `get_short_epg` does
  not become friendly on request 14.
* **a short TTL cache in front of it all** (`SPM_EPG_NOW_TTL`, default 120 s), so a
  user scrolling a channel list and a user leaving the tab open cost the same.

The timestamps a portal returns carry no timezone, which is why `stb_timezone` (the
cookie we send) is what they are read as - see `app/portal/epg.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy import select

from ..database import SessionLocal
from ..models import (EpgProgramme, LivePlaylist, LivePlaylistSource, LiveSource,
                      MacAddress, Portal)
from ..portal.account import mac_is_usable
from ..portal.capabilities import gate_feature, loads_modules
from ..portal.client import PortalError
from ..portal.epg import pick_now
from ..portal.identity import STB_TIMEZONE
from ..portal.pool import POOL, PortalSession
from .db_logging import db_log

log = logging.getLogger("spm.epgnow")

#: how long an answer stays fresh. Two minutes is "live enough" for a tooltip and
#: long enough that paging through 50 channels is one request per channel *once*.
TTL_SECONDS = max(15, int(os.environ.get("SPM_EPG_NOW_TTL", "120") or "120"))
#: a batch is what a visible page is; the cap is what stops a caller walking the
#: whole catalogue by accident (or by a bug in the GUI's page size)
MAX_IDS = 60
#: how many channels one portal is asked about at a time, and how many times it may
#: disappoint us before the rest of the batch is skipped for its own good
CONCURRENCY = 6
MAX_PORTAL_FAILURES = 3
#: a 503/timeout is retried this many times *within* a batch, per channel
RETRIES = 2

#: (portal_id, ch_id) -> (monotonic deadline, payload)
_CACHE: dict[tuple[int, str], tuple[float, dict]] = {}


def cache_clear() -> int:
    n = len(_CACHE)
    _CACHE.clear()
    return n


def _retryable(exc: Exception) -> bool:
    """Retry a portal that was busy, never one that said no.

    `PortalError.code` is the distinction R4 bought, and there is no HTTP status on
    the object to fall back on: a transport 503 or a timeout is worth another try a
    few hundred ms later, while `no such action`, a 403 or `bad_json` means this
    panel does not answer *this* call, and asking again is how a polite proxy turns
    into a load problem. Deliberately short - `empty_reply` is in, `no_url` is not.
    """
    if isinstance(exc, PortalError):
        return exc.code in RETRY_CODES
    return isinstance(exc, (asyncio.TimeoutError, OSError))


#: the transient half of the portal error taxonomy (see `status_for_error`)
RETRY_CODES = frozenset({"timeout", "transport", "empty_reply",
                         "http_429", "http_500", "http_502", "http_503", "http_504"})


def _as_utc(value: datetime | None) -> datetime | None:
    """Re-attach the offset a database read may or may not have brought back.

    Programme rows are written as UTC-aware (`_xmltv_ts`), but SQLite returns naive
    datetimes and Postgres returns aware ones, and `datetime.now(timezone.utc) -
    naive` is a TypeError deep inside a tooltip lookup. Normalising on read is what
    keeps one code path for both backends; assuming the column type told us the
    zone is what makes it work in development and crash in production.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


async def _xmltv_now(tvg_id: str, now: datetime) -> dict | None:
    """The free answer: a programme row from the configured XMLTV source."""
    if not tvg_id:
        return None
    async with SessionLocal() as s:
        row = (await s.execute(select(EpgProgramme).where(
            EpgProgramme.tvg_id == tvg_id,
            EpgProgramme.start_ts <= now, EpgProgramme.stop_ts > now)
            .order_by(EpgProgramme.start_ts.desc()).limit(1))).scalars().first()
        nxt = (await s.execute(select(EpgProgramme).where(
            EpgProgramme.tvg_id == tvg_id, EpgProgramme.start_ts > now)
            .order_by(EpgProgramme.start_ts.asc()).limit(1))).scalars().first()
    # both queries keep `.scalars()` *outside* the awaited parens: `await` binds to
    # the whole trailer chain, so `await s.execute(sel).scalars()` calls `.scalars()`
    # on a coroutine - an AttributeError on a query, in code no test without a
    # database reaches.
    if row is None and nxt is None:
        return None
    for p in (row, nxt):
        if p is not None:
            p.start_ts = _as_utc(p.start_ts)
            p.stop_ts = _as_utc(p.stop_ts)

    def _d(p) -> dict:
        return {"title": p.title, "start": p.start_ts.isoformat(timespec="minutes"),
                "stop": p.stop_ts.isoformat(timespec="minutes"),
                "description": (p.desc or "")[:400], "has_archive": False}
    out = {"found": row is not None, "source": "xmltv"}
    if row is not None:
        total = max(1.0, (row.stop_ts - row.start_ts).total_seconds())
        pct = max(0, min(100, round((now - row.start_ts).total_seconds() / total * 100)))
        out["now"] = dict(_d(row), progress=pct,
                          minutes_left=max(0, round((row.stop_ts - now).total_seconds() / 60)))
    else:
        out["now"] = None
    if nxt is not None:
        out["next"] = dict(_d(nxt), starts_in=round((nxt.start_ts - now).total_seconds() / 60))
    return out


async def _targets(ids: list[int], kind: str) -> dict[int, dict]:
    """The rows a batch will ask about, in one query per kind.

    `kind="live"` is a fetched portal channel (asks by `portal_channel_id`);
    `kind="playlist"` is an output item, whose first live source is what carries the
    portal ids and whose own `epg_id` is the XMLTV key - so a tooltip on the
    playlist page can be answered entirely from the database for most users.
    """
    out: dict[int, dict] = {}
    async with SessionLocal() as s:
        if kind == "playlist":
            rows = (await s.execute(select(LivePlaylist).where(
                LivePlaylist.id.in_(ids)))).scalars().all()
            for it in rows:
                src = None
                link = (await s.execute(select(LivePlaylistSource).where(
                    LivePlaylistSource.live_playlist_id == it.id)
                    .order_by(LivePlaylistSource.priority)
                    .limit(1))).scalars().first()
                if link:
                    src = await s.get(LiveSource, link.live_source_id)
                if src is None:
                    out[it.id] = {"found": False, "source": "none",
                                  "why": "this item has no portal source to ask"}
                    continue
                out[it.id] = {"portal_id": src.portal_id, "ch_id": src.portal_channel_id,
                               "tvg_id": it.epg_id or src.epg_original or "",
                               "name": it.custom_name or src.original_name}
        else:
            rows = (await s.execute(select(LiveSource).where(
                LiveSource.id.in_(ids)))).scalars().all()
            for src in rows:
                out[src.id] = {"portal_id": src.portal_id, "ch_id": src.portal_channel_id,
                               "tvg_id": src.epg_original or "", "name": src.original_name}
    return out


async def now_for(live_ids: list[int] | None = None, playlist_ids: list[int] | None = None,
                  *, refresh: bool = False) -> dict:
    """The answer for a page of rows: `{ "<kind>:<id>": {now, next, source, …} }`.

    Never raises. A tooltip that fails is a tooltip that shows nothing, and the
    reason belongs in the response (`why`) and in the log, not in a 500.
    """
    now = datetime.now(timezone.utc)
    wanted: list[tuple[str, int]] = ([("live", i) for i in live_ids or []]
                                     + [("playlist", i) for i in playlist_ids or []])
    # deduped per batch, and bounded: see the module docstring
    seen: set[tuple[str, int]] = set()
    pairs = [(k, i) for (k, i) in wanted if i and (k, i) not in seen and not seen.add((k, i))]
    truncated = max(0, len(pairs) - MAX_IDS)
    pairs = pairs[:MAX_IDS]

    out: dict[str, dict] = {}
    counts = {"xmltv": 0, "cache": 0, "asked": 0, "skipped": 0}
    by_key: dict[tuple[str, int], dict] = {}
    for kind, ids in (("live", [i for k, i in pairs if k == "live"]),
                      ("playlist", [i for k, i in pairs if k == "playlist"])):
        if ids:
            by_key.update({(kind, key): val for key, val in (await _targets(ids, kind)).items()})

    # pass 1: anything we already know costs nothing
    need: dict[int, list[tuple[str, int, dict]]] = {}
    for key in pairs:
        t = by_key.get(key)
        if t is None:
            out[f"{key[0]}:{key[1]}"] = {"found": False, "source": "none",
                                        "why": "no such row"}
            counts["skipped"] += 1
            continue
        if "why" in t:                                  # unresolvable row, said already
            out[f"{key[0]}:{key[1]}"] = dict(t)
            counts["skipped"] += 1
            continue
        hit = _CACHE.get((t["portal_id"], str(t["ch_id"])))
        if hit and not refresh and hit[0] > time.monotonic():
            out[f"{key[0]}:{key[1]}"] = dict(hit[1])
            counts["cache"] += 1
            continue
        guide = await _xmltv_now(t["tvg_id"], now)
        # any answer from the configured guide counts, including "nothing is on
        # now, X starts at 21:00": the guide knows the schedule, so asking the
        # portal to confirm a gap is a request spent proving nothing
        if guide:
            out[f"{key[0]}:{key[1]}"] = guide
            counts["xmltv"] += 1
            continue
        need.setdefault(t["portal_id"], []).append((key[0], key[1], t))

    # pass 2: one portal session per portal, one request per channel that still needs it
    async def ask_portal(portal_id: int, items: list[tuple[str, int, dict]]) -> None:
        async with SessionLocal() as s:
            portal = await s.get(Portal, portal_id)
            if portal is None or not portal.enabled:
                for kind, i, t in items:
                    out[f"{kind}:{i}"] = {"found": False, "source": "none",
                                          "why": "portal disabled or deleted"}
                    counts["skipped"] += 1
                return
            mac = (await s.execute(select(MacAddress).where(MacAddress.portal_id == portal_id)
                                   .order_by(MacAddress.order))).scalars().all()
        usable = next((m for m in mac if mac_is_usable(m.status)), mac[0] if mac else None)
        if usable is None:
            for kind, i, t in items:
                out[f"{kind}:{i}"] = {"found": False, "source": "none",
                                      "why": "this portal has no MAC to ask with"}
                counts["skipped"] += 1
            return
        ok, why = gate_feature(loads_modules(portal.modules), "epg")
        if not ok:
            # R6's answer, used: a panel that says it has no `epg` module does not
            # have a guide to read, and we should not spend a request per channel
            # discovering that.
            for kind, i, t in items:
                out[f"{kind}:{i}"] = {"found": False, "source": "none", "why": why,
                                      "gated": True}
                counts["skipped"] += 1
            return

        client = await POOL.get(PortalSession.from_rows(portal, usable,
                                                       portal_url=portal.resolved_url
                                                       or portal.base_url))
        sem = asyncio.Semaphore(CONCURRENCY)
        failures = 0
        lock = asyncio.Lock()
        try:
            async def one(kind: str, i: int, t: dict) -> None:
                nonlocal failures
                async with lock:
                    if failures >= MAX_PORTAL_FAILURES:
                        out[f"{kind}:{i}"] = {
                            "found": False, "source": "none",
                            "why": f"not asked: this portal already failed {failures} "
                                   f"short-epg requests in this batch"}
                        counts["skipped"] += 1
                        return
                async with sem:
                    payload: dict = {}
                    attempt = 0
                    while True:
                        try:
                            progs = await client.short_epg(
                                t["ch_id"], size=8, tz=_tz_of(portal))
                            payload = dict(pick_now(progs))
                            payload.update({"found": bool(payload.get("now")),
                                            "source": "portal",
                                            "name": t.get("name") or ""})
                            break
                        except Exception as exc:  # noqa: BLE001 - a tooltip may not raise
                            code = getattr(exc, "code", "") or type(exc).__name__
                            if attempt < RETRIES and _retryable(exc):
                                attempt += 1
                                await asyncio.sleep(0.3 * attempt)
                                continue
                            payload = {"found": False, "source": "none",
                                       "why": f"{type(exc).__name__}: {code}"}
                            break
                if payload.get("source") == "none":
                    async with lock:
                        failures += 1
                elif payload.get("source") == "portal":
                    async with lock:
                        counts["asked"] += 1
                _CACHE[(t["portal_id"], str(t["ch_id"]))] = (time.monotonic() + TTL_SECONDS,
                                                              payload)
                out[f"{kind}:{i}"] = dict(payload)

            await asyncio.gather(*(one(k, i, t) for k, i, t in items))
        finally:
            await client.close()
        if failures:
            await db_log("WARNING", "epg",
                         f"[{portal.name}] short-epg: {failures} of {len(items)} channels "
                         f"unanswered (per-channel reason in the API response; the panel "
                         f"may not implement get_short_epg)")

    await asyncio.gather(*(ask_portal(pid, items) for pid, items in need.items()))

    if any(counts.values()):
        await db_log("INFO", "epg",
                     f"what's-on-now: {counts['asked']} asked, {counts['xmltv']} from the "
                     f"guide source, {counts['cache']} cached, {counts['skipped']} skipped"
                     + (f", batch truncated at {MAX_IDS}" if truncated else ""))
    return {"items": out, "ttl": TTL_SECONDS, "counts": counts,
            "truncated": truncated, "at": now.isoformat(timespec="seconds")}


def _tz_of(portal):
    """The timezone the portal will have rendered its guide times in.

    Same value we put in the `timezone=` cookie, because that is *why* the panel
    chose them: a portal asked in Amsterdam answers in Amsterdam. Falling back to
    the server default keeps a portal row without an explicit override working.
    """
    from zoneinfo import ZoneInfo
    name = str(getattr(portal, "stb_timezone", "") or STB_TIMEZONE or "").strip()
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a tz name the OS does not know is not fatal
        log.debug("unknown stb_timezone %r; reading portal times as UTC", name)
        return timezone.utc
