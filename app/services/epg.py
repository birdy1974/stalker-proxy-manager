"""
EPG ingestion, fuzzy channel matching and merged XMLTV output (Phase 3).

Flow:
  1. enabled epg_sources are downloaded (plain XML, .gz or .xz) and parsed:
     - every <channel> is upserted into epg_channels
     - <programme> rows are kept ONLY for tvg_ids already matched to our
       live playlist channels (bounded storage), within now-6h..+7d
  2. auto-match: live_playlist rows with an empty epg_id are fuzzy-matched
     against lexicographically-normalized channel names (case-insensitive,
     HD/4K/(NL) suffix-stripped). Manually set epg_ids are never touched.
  3. build_xmltv() merges ordered live channels (per user groups) with their
     programmes into a spec XMLTV document served at /xmltv.php and /epg.xml.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import lzma
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import httpx
from sqlalchemy import delete, func, select

from ..database import SessionLocal
from ..models import EpgChannel, EpgProgramme, EpgSource, LivePlaylist
from ..services.db_logging import db_log

_RE_POORSON = re.compile(
    r"\b(hd|fhd|uhd|4k|8k|sd|hq|hq tv|tv|channels?|hd\+|hd 1080|1080p|720p|nl|nld|ned|be|uk)\b",
    re.I)
_RE_TAIL = re.compile(r"\s*[\[\(][^\]\)]*[\]\)]\s*$")


def norm_name(name: str) -> str:
    """Aggressively normalized channel name for EPG/logo matching."""
    x = (name or "").lower().strip()
    x = _RE_TAIL.sub("", x).strip()
    x = re.sub(r"[.\-_+,/\\|&']", " ", x)
    x = _RE_POORSON.sub(" ", x)
    # "npo1" (tv-logos filename style) <-> "npo 1" (guide/playlist style):
    # split at letter/digit boundaries AFTER the quality-word strip (so "4k"
    # is still removed as one token)
    x = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", x)
    return re.sub(r"\s{2,}", " ", x).strip()


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _xmltv_ts(s: str) -> datetime | None:
    m = re.match(r"(\d{14})\s*([+-]\d{4})?", (s or "").strip())
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    off = m.group(2)
    if off:
        sign, hh, mm = 1 if off[0] == "+" else -1, int(off[1:3]), int(off[3:5])
        dt = (dt - timedelta(hours=hh, minutes=mm) * sign).replace(tzinfo=timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


async def _fetch(xml_url: str) -> bytes:
    from .http_client import outbound_client
    async with outbound_client(timeout=httpx.Timeout(60, connect=15)) as c:
        r = await c.get(xml_url)
        r.raise_for_status()
        data = r.content
    if xml_url.endswith(".gz") or data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if xml_url.endswith(".xz") or data[:6] == b"\xfd7zXZ\x00":
        return lzma.decompress(data)
    return data


# --------------------------------------------------------------------------- #
async def match_epg_to_playlist(progress=None) -> int:
    """Fuzzy-match unmatched live playlist channels to EPG channel names.
    Returns the count of newly matched rows."""
    async with SessionLocal() as s:
        targets = (await s.execute(
            select(LivePlaylist).where(LivePlaylist.epg_id.is_(None) | (LivePlaylist.epg_id == ""))
        )).scalars().all()
        rows = (await s.execute(select(EpgChannel))).scalars().all()
    if not targets or not rows:
        return 0
    # best normalized name per tvg_id
    cand: dict[str, tuple[str, str]] = {}      # tvg_id -> (norm, display)
    for r in rows:
        n = norm_name(r.name)
        if r.tvg_id not in cand or len(n) > len(cand[r.tvg_id][0]):
            cand[r.tvg_id] = (n, r.name)
    matched = 0
    async with SessionLocal() as s:
        items = (await s.execute(select(LivePlaylist).where(
            LivePlaylist.epg_id.is_(None) | (LivePlaylist.epg_id == "")))).scalars().all()
        for it in items:
            want = norm_name(it.custom_name)
            if not want:
                continue
            best_id, best_score = None, 0.0
            for tvg_id, (n, _disp) in cand.items():
                sc = _sim(want, n)
                if sc > best_score:
                    best_id, best_score = tvg_id, sc
            if best_id and (best_score == 1.0 or best_score >= 0.86):
                it.epg_id = best_id
                matched += 1
            if progress and matched % 20 == 0:
                await progress(f"match {matched}")
        await s.commit()
    if matched:
        await db_log("INFO", "epg", f"auto-match: {matched} playlist channels got an EPG id")
    return matched


async def refresh_source(src_id: int) -> dict:
    """Download + parse one EPG source (channels always, programmes only for
    matched tvg_ids). Returns a small report. Serialized via INGEST_LOCK."""
    async with INGEST_LOCK:
        return await _refresh_source_locked(src_id)


async def _refresh_source_locked(src_id: int) -> dict:
    async with SessionLocal() as s:
        src = await s.get(EpgSource, src_id)
        found = src is not None and src.enabled
        url = src.url if src else ""
    if not found:
        return {"ok": False, "error": "source not found or disabled"}

    await db_log("INFO", "epg", f"downloading {url} …")
    try:
        raw = await _fetch(url)
    except Exception as exc:  # noqa: BLE001
        async with SessionLocal() as s:
            r = await s.get(EpgSource, src_id)
            r.status = f"download failed: {exc}"
            await s.commit()
        await db_log("ERROR", "epg", f"{url}: download failed: {exc}")
        return {"ok": False, "error": str(exc)}

    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(hours=6), now + timedelta(days=7)
    n_chan, n_prog = 0, 0
    chan_rows: list[dict] = []

    # ---- pass 1: channels only (must land in the DB before matching) -------
    # NB: only clear the ITEM elements - clearing every child as it ends would
    # wipe <display-name> texts before the enclosing <channel> end event.
    for _event, elm in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if elm.tag == "channel":
            cid = elm.attrib.get("id")
            names = [el.text or "" for el in elm.findall("display-name")]
            icon = elm.find("icon")
            if cid:
                chan_rows.append({"epg_source_id": src_id, "tvg_id": cid[:200],
                                  "name": (names[0] if names else cid)[:300],
                                  "icon": (icon.attrib.get("src") if icon is not None else None)})
                n_chan += 1
            elm.clear()
        elif elm.tag == "programme":
            elm.clear()

    async with SessionLocal() as s:
        for r in chan_rows:
            row = (await s.execute(select(EpgChannel).where(
                EpgChannel.epg_source_id == src_id,
                EpgChannel.tvg_id == r["tvg_id"]))).scalar_one_or_none()
            if row is None:
                s.add(EpgChannel(**r))
            else:
                row.name, row.icon = r["name"], r["icon"]
        src = await s.get(EpgSource, src_id)
        src.last_fetch = now
        src.channel_count = n_chan
        src.status = f"ok — {n_chan} channels"
        await s.commit()

    # ---- match BEFORE the programme pass so brand-new channel ids are in scope
    matched = await match_epg_to_playlist()
    wanted: set[str] = set()
    async with SessionLocal() as s:
        wanted.update(x for (x,) in (await s.execute(
            select(LivePlaylist.epg_id).where(LivePlaylist.epg_id.isnot(None)))).all() if x)

    # ---- pass 2: programmes (keep wanted ids within the bounded window only)
    for _event, elm in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if elm.tag == "programme":
            tvg = (elm.attrib.get("channel") or "")[:200]
            st = _xmltv_ts(elm.attrib.get("start") or "")
            en = _xmltv_ts(elm.attrib.get("stop") or "")
            if tvg and st and en and tvg in wanted and lo <= st <= hi:
                ttl = (elm.findtext("title") or "").strip()[:400]
                if ttl:
                    n_prog += 1
                    cat = elm.findtext("category")
                    icon = elm.find("icon")
                    PROG_BUFFER.append({
                        "tvg_id": tvg, "start_ts": st, "stop_ts": en, "title": ttl,
                        "sub_title": ((elm.findtext("sub-title") or "").strip()[:400] or None),
                        "desc": ((elm.findtext("desc") or "").strip() or None),
                        "category": ((cat or "").strip()[:200] or None) if cat else None,
                        "icon": (icon.attrib.get("src") if icon is not None else None)})
                    if len(PROG_BUFFER) >= 500:
                        await _flush_programmes()
            elm.clear()
        elif elm.tag == "channel":
            elm.clear()

    await _flush_programmes(final=True)
    async with SessionLocal() as s:
        res = await s.execute(delete(EpgProgramme).where(EpgProgramme.stop_ts < now))
        await s.commit()
        pruned = res.rowcount or 0
    msg = (f"{url}: {n_chan} EPG channels, {n_prog} matching programmes ingested, "
           f"{pruned} pruned, {matched} auto-matches")
    await db_log("INFO", "epg", msg)
    return {"ok": True, "channels": n_chan, "programmes": n_prog, "matched": matched}


PROG_BUFFER: list[dict] = []
INGEST_LOCK = asyncio.Lock()      # serializes refreshes: one writer of the global buffer


async def _flush_programmes(final: bool = False) -> None:
    """Batch-insert buffered programme rows (dedup via natural unique key)."""
    global PROG_BUFFER
    buf, PROG_BUFFER = PROG_BUFFER, []
    if not buf:
        return
    async with SessionLocal() as s:
        for r in buf:
            exists = await s.scalar(select(func.count()).select_from(EpgProgramme).where(
                EpgProgramme.tvg_id == r["tvg_id"], EpgProgramme.start_ts == r["start_ts"],
                EpgProgramme.title == r["title"]))
            if exists:
                continue
            s.add(EpgProgramme(**r))
        await s.commit()


async def refresh_all() -> dict:
    async with SessionLocal() as s:
        ids = [x.id for x in (await s.execute(
            select(EpgSource).where(EpgSource.enabled.is_(True)))).scalars().all()]
    out = {"sources": len(ids), "results": []}
    for i in ids:
        out["results"].append(await refresh_source(i))
    return out


async def build_xmltv(base_url: str, user) -> str:
    """Merge the user's visible live channels with EPG programmes."""
    from ..services.playlist_gen import _allowed, _groups  # reuse user filters

    groups = _groups(user)
    now = datetime.now(timezone.utc)
    hi = now + timedelta(hours=48)

    async with SessionLocal() as s:
        items = (await s.execute(select(LivePlaylist).where(LivePlaylist.enabled.is_(True))
                                 .order_by(LivePlaylist.order, LivePlaylist.id))).scalars().all()
        chans = []
        for it in items:
            if not _allowed(it.group_name, groups["live"]):
                continue
            cid = it.epg_id or re.sub(r"[^A-Za-z0-9_.-]+", ".", it.custom_name)
            chans.append((it, cid))
        ids = {cid for _, cid in chans}
        progs = (await s.execute(
            select(EpgProgramme).where(EpgProgramme.tvg_id.in_(ids) if ids else False,
                                       EpgProgramme.stop_ts >= now,
                                       EpgProgramme.start_ts <= hi)
            .order_by(EpgProgramme.tvg_id, EpgProgramme.start_ts))).scalars().all()

    by_id: dict[str, list[EpgProgramme]] = {}
    for p in progs:
        by_id.setdefault(p.tvg_id, []).append(p)

    def esc(t: str | None) -> str:
        from xml.sax.saxutils import escape as _e
        return _e(t or "")

    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
         '<tv generator-info-name="stalker-proxy-manager">']
    for it, cid in chans:
        L.append(f'  <channel id="{esc(cid)}">')
        L.append(f"    <display-name>{esc(it.custom_name)}</display-name>")
        if it.logo:
            logo = it.logo if it.logo.startswith("http") else f"{base_url}{it.logo}"
            L.append(f'    <icon src="{esc(logo)}"/>')
        L.append("  </channel>")
    for it, cid in chans:
        for p in by_id.get(cid, []):
            L.append(f'  <programme start="{_fmt_ts(p.start_ts)}" stop="{_fmt_ts(p.stop_ts)}" channel="{esc(cid)}">')
            L.append(f"    <title>{esc(p.title)}</title>")
            if p.sub_title:
                L.append(f"    <sub-title>{esc(p.sub_title)}</sub-title>")
            if p.desc:
                L.append(f"    <desc>{esc(p.desc)}</desc>")
            if p.category:
                L.append(f"    <category>{esc(p.category)}</category>")
            if p.icon:
                L.append(f'    <icon src="{esc(p.icon)}"/>')
            L.append("  </programme>")
    L.append("</tv>")
    return "\n".join(L) + "\n"


# ------------------------------- scheduler ----------------------------------
async def epg_scheduler(parse=60 * 60) -> None:
    """Refresh due sources in the background on the configured interval."""
    from ..services.playlist_gen import get_setting

    while True:
        try:
            hours = int(await get_setting("epg_refresh_hours", 24) or 24)
            async with SessionLocal() as s:
                rows = (await s.execute(select(EpgSource).where(
                    EpgSource.enabled.is_(True)))).scalars().all()
                due = [r.id for r in rows if not r.last_fetch
                       or (datetime.now(timezone.utc) - (
                           r.last_fetch if r.last_fetch.tzinfo else
                           r.last_fetch.replace(tzinfo=timezone.utc))).total_seconds() > hours * 3600]
            for i in due:
                await refresh_source(i)
        except Exception as exc:  # noqa: BLE001 - scheduler must not die
            await db_log("ERROR", "epg", f"scheduler tick failed: {exc}")
        await asyncio.sleep(parse)
