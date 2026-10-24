"""
EPG ingestion, fuzzy channel matching and merged XMLTV output (Phase 3).

Flow:
  1. enabled epg_sources are downloaded (plain XML, .gz or .xz) and parsed:
     - every <channel> is upserted into epg_channels
     - <programme> rows are kept ONLY for tvg_ids already matched to our
       live playlist channels or explicit guide mappings (bounded storage),
       within now-78h..+7d
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
import json
import hashlib
import uuid
import lzma
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from types import SimpleNamespace

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..database import SessionLocal, engine
from ..models import EpgChannel, EpgProgramme, EpgSource, LivePlaylist, Portal, Setting
from ..models import EpgChannelSource
from ..config import DATA_DIR
from ..services.db_logging import db_log
from .epg_timing import programme_times, timing_key, parse_time

_RE_POORSON = re.compile(
    r"\b(hd|fhd|uhd|4k|8k|sd|hq|hq tv|tv|channels?|hd\+|hd 1080|1080p|720p|nl|nld|ned|be|uk)\b",
    re.I,
)
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


# Rows per grouped database round trip. EPG files easily carry tens of
# thousands of channels/programmes: one SELECT per row is what used to make an
# import take minutes.
EPG_CHUNK = 500


def _chunked(seq: list, size: int = EPG_CHUNK):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _upsert_stmt(
    model,
    rows: list[dict],
    index_elements: list[str],
    update_cols: list[str] | None = None,
):
    """
    ONE statement for a whole chunk - no "SELECT, then INSERT" round trip per
    row. Uses the native upsert of the running engine (Postgres in production,
    SQLite in dev); conflicting rows are updated or skipped inside the database.
    """
    insert = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
    stmt = insert(model).values(rows)
    if update_cols:
        return stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={c: getattr(stmt.excluded, c) for c in update_cols},
        )
    return stmt.on_conflict_do_nothing(index_elements=index_elements)


async def _upsert_channels(src_id: int, chan_rows: list[dict]) -> int:
    """
    Upsert parsed <channel> rows in GROUPS: one statement per chunk instead of
    one SELECT (plus autoflush) per row - a real guide carries 10k+ channels.
    """
    if not chan_rows:
        return 0
    by_key: dict[str, dict] = {}
    for r in chan_rows:  # last occurrence wins per tvg_id
        by_key[r["tvg_id"]] = r
    rows = list(by_key.values())
    async with SessionLocal() as s:
        for chunk in _chunked(rows):
            await s.execute(
                _upsert_stmt(
                    EpgChannel, chunk, ["epg_source_id", "tvg_id"], ["name", "icon"]
                )
            )
            await s.commit()
    return len(rows)


def _xmltv_ts(s: str) -> datetime | None:
    try:
        return parse_time(s).utc
    except (ValueError, OverflowError):
        return None


def _fmt_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


DEFAULT_EPG_URLS = (
    "http://www.xmltvepg.nl/rytecNL_Basic.xz",
    "http://rytecepg.wanwizard.eu/rytecNL_Basic.xz",
    "http://epg.vuplus-community.net/rytecNL_Basic.xz",
)
MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_XML = 256 * 1024 * 1024


async def ensure_default_sources():
    """One-time upgrade: enable the requested feeds, then honour later user edits."""
    async with SessionLocal() as db:
        if await db.get(Setting, "epg_defaults_v1"):
            return
        for url in DEFAULT_EPG_URLS:
            row = await db.scalar(select(EpgSource).where(EpgSource.url == url))
            if row is None:
                db.add(EpgSource(url=url, enabled=True))
            else:
                row.enabled = True
        db.add(Setting(key="epg_defaults_v1", value="true"))
        await db.commit()


async def ensure_portal_sources():
    from .runtime_settings import get_setting

    if not await get_setting("epg_portal_enabled", True):
        return
    async with SessionLocal() as db:
        portals = (
            (await db.execute(select(Portal).where(Portal.enabled.is_(True))))
            .scalars()
            .all()
        )
        known = set((await db.scalars(select(EpgSource.portal_id))).all())
        for portal in portals:
            if portal.id not in known:
                db.add(
                    EpgSource(
                        url="portal://" + uuid.uuid4().hex,
                        portal_id=portal.id,
                        enabled=True,
                    )
                )
        await db.commit()


def _decode(data: bytes) -> bytes:
    if data[:6] == b"\xfd7zXZ\x00":
        stream = lzma.LZMAFile(io.BytesIO(data))
    elif data[:2] == b"\x1f\x8b":
        stream = gzip.GzipFile(fileobj=io.BytesIO(data))
    else:
        if len(data) > MAX_XML:
            raise ValueError(
                "XMLTV exceeds 256 MiB limit; use a country-specific guide"
            )
        return data
    with stream:
        raw = stream.read(MAX_XML + 1)
    if len(raw) > MAX_XML:
        raise ValueError("Expanded XMLTV exceeds 256 MiB limit")
    return raw


async def _fetch(xml_url: str) -> bytes:
    from .http_client import outbound_client

    async with outbound_client(timeout=httpx.Timeout(60, connect=15)) as c:
        async with c.stream("GET", xml_url) as response:
            response.raise_for_status()
            data = bytearray()
            async for part in response.aiter_bytes():
                data.extend(part)
                if len(data) > MAX_DOWNLOAD:
                    raise ValueError(
                        "EPG download exceeds 50 MiB; use a smaller country feed"
                    )
    return await asyncio.to_thread(_decode, bytes(data))


def _source_key(url):
    return hashlib.sha256(url.encode()).hexdigest()[:20]


def _cache_path(src_id, url):
    return DATA_DIR / "epg-cache" / f"{int(src_id)}-{_source_key(url)}.xml.gz"


def _cache_write(src_id, url, raw):
    path = _cache_path(src_id, url)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(raw))
    tmp.replace(path)


async def epg_candidates(db):
    rows = (
        await db.execute(
            select(EpgChannel, EpgSource)
            .join(EpgSource, EpgChannel.epg_source_id == EpgSource.id)
            .where(EpgSource.enabled.is_(True))
            .order_by(EpgChannel.tvg_id, EpgSource.id)
        )
    ).all()
    from .runtime_settings import get_setting

    portal_on = await get_setting("epg_portal_enabled", True)
    active_portals = set(
        (await db.scalars(select(Portal.id).where(Portal.enabled.is_(True)))).all()
    )
    candidates = {}
    for channel, source in rows:
        if source.url.startswith("portal://") and (
            not portal_on or source.portal_id not in active_portals
        ):
            continue
        candidate = candidates.setdefault(
            channel.tvg_id,
            {
                "tvg_id": channel.tvg_id,
                "name": channel.name,
                "names": [],
                "sources": [],
                "source_ids": [],
            },
        )
        candidate["names"].append(channel.name)
        candidate["sources"].append("Portal guide" if source.portal_id else source.url)
        candidate["source_ids"].append(source.id)
    return list(candidates.values())


def _prepare_candidates(candidates):
    """Normalize every candidate alias ONCE for a batch of rankings.

    Ranking used to re-run norm_name (six regex passes) plus digit/plus
    extraction on every alias of every candidate for every playlist item -
    O(items x candidates) regex work before any similarity scoring. 300
    unmatched items against a 3000-channel guide measured ~15 s of CPU.
    """
    prepared = []
    for candidate in candidates:
        aliases = []
        for alias in candidate["names"]:
            normalized = norm_name(alias.replace("+", " plus "))
            aliases.append((normalized, re.findall(r"\d+", normalized),
                            "plus" in normalized.split()))
        prepared.append((candidate, aliases))
    return prepared


def _rank_prepared(name, prepared, limit=20):
    # '+' denotes a distinct channel, especially Viaplay TV+, not decoration.
    want = norm_name((name or "").replace("+", " plus "))
    numbers = re.findall(r"\d+", want)
    want_plus = "plus" in want.split()
    want_len = len(want)
    matcher = SequenceMatcher(None, want, "")
    ranked = []
    for candidate, aliases in prepared:
        score = 0.0
        for normalized, digits, has_plus in aliases:
            if not want or not normalized:
                continue          # _sim() scored empty strings 0.0
            factor = 1.0
            if numbers != digits:
                factor *= 0.5  # do not guess NPO 2 for NPO 1 / Viaplay 2 for 1
            if want_plus != has_plus:
                factor *= 0.5
            # difflib's quick ratios are documented UPPER bounds on ratio().
            # When even the bound (penalties applied) can neither beat the
            # running score nor reach the 0.62 keep-threshold, the exact
            # ratio cannot change which candidates are kept or their final
            # scores - skip it. Most candidates are nowhere near, so this
            # skips ~all of the full SequenceMatcher work on a guide-sized
            # run while producing byte-identical output. The loosest bound
            # (lengths only - exactly real_quick_ratio's formula) goes first
            # because it needs no matcher work at all.
            norm_len = len(normalized)
            bound = factor * (2.0 * min(want_len, norm_len)
                              / (want_len + norm_len))
            if bound < 0.62 or bound <= score:
                continue
            matcher.set_seq2(normalized)
            if factor * matcher.quick_ratio() < 0.62 \
                    or factor * matcher.quick_ratio() <= score:
                continue
            score = max(score, factor * matcher.ratio())
        if score >= 0.62:
            ranked.append(
                {k: v for k, v in candidate.items() if k != "names"}
                | {"score": round(score, 4)}
            )
    ranked.sort(key=lambda row: (-row["score"], row["tvg_id"]))
    return ranked[:limit]


def rank_candidates(name, candidates, limit=20):
    return _rank_prepared(name, _prepare_candidates(candidates), limit)


def _match_items(plain_items, candidates, review_all):
    """Pure-CPU half of match_report: decide assignments, touch no database.

    plain_items: (id, custom_name, epg_id, epg_sources_explicit) tuples.
    Returns (assignments {item_id: tvg_id}, ambiguous, unmatched) - the same
    decisions the old inline loop made, computed where they belong.
    """
    prepared = _prepare_candidates(candidates)
    known = {row["tvg_id"] for row in candidates}
    assignments, ambiguous, unmatched = {}, [], 0
    for item_id, name, epg_id, explicit in plain_items:
        if explicit:
            continue
        if epg_id in known and not review_all:
            continue
        choices = _rank_prepared(name, prepared)
        if not choices:
            unmatched += 1
            continue
        if not epg_id and len(choices) == 1 and choices[0]["score"] >= 0.86:
            assignments[item_id] = choices[0]["tvg_id"]
        else:
            ambiguous.append(
                {
                    "id": item_id,
                    "name": name,
                    "epg_id": epg_id,
                    "candidates": choices,
                }
            )
    return assignments, ambiguous, unmatched


async def match_report(review_all=False, progress=None):
    matched, ambiguous, unmatched = 0, [], 0
    async with SessionLocal() as db:
        candidates = await epg_candidates(db)
        items = (
            (
                await db.execute(
                    select(LivePlaylist)
                    .where(LivePlaylist.enabled.is_(True))
                    .order_by(LivePlaylist.order, LivePlaylist.id)
                )
            )
            .scalars()
            .all()
        )
        plain = [
            (it.id, it.custom_name, it.epg_id, it.epg_sources_explicit)
            for it in items
        ]
        by_id = {it.id: it for it in items}
        # The ranking is O(items x candidates) difflib work - seconds of
        # pure CPU on a full guide, even with the quick-ratio skips. It used
        # to run inline here, on the event loop, inside every EPG refresh:
        # streams, watchdogs and the GUI froze for its duration. The session
        # stays open across the thread hop (it did before too, while ranking
        # inline), but nothing in the worker thread touches it or the ORM
        # objects - decisions come back as plain data and are applied here.
        assignments, ambiguous, unmatched = await asyncio.to_thread(
            _match_items, plain, candidates, review_all
        )
        for item_id, tvg_id in assignments.items():
            by_id[item_id].epg_id = tvg_id
            matched += 1
        await db.commit()
    if matched:
        from .playlist_gen import clear_m3u_cache

        clear_m3u_cache()
        await db_log(
            "INFO",
            "epg",
            f"Matched {matched} unambiguous channels; {len(ambiguous)} need review",
        )
    return {"matched": matched, "ambiguous": ambiguous, "unmatched": unmatched}


async def match_epg_to_playlist(progress=None) -> int:
    return (await match_report(progress=progress))["matched"]


def _parse_channels(raw: bytes, src_id: int) -> tuple[dict, list[dict]]:
    """Pass 1 of an XMLTV ingest as pure CPU work: validate the document and
    collect its channel rows. Runs in a worker thread - walked on the event
    loop, a tens-of-MB guide froze streams, watchdogs and the GUI for
    seconds per pass (there are two passes per source, per refresh)."""
    # Reject HTML/error pages even if they happen to be well-formed XML.
    first = next(ET.iterparse(io.BytesIO(raw), events=("start",)), None)
    if first is None or first[1].tag != "tv":
        raise ValueError("Response is not an XMLTV <tv> document")
    root_attrs = dict(first[1].attrib)
    chan_rows: list[dict] = []
    # NB: only clear the ITEM elements - clearing every child as it ends would
    # wipe <display-name> texts before the enclosing <channel> end event.
    for _event, elm in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if elm.tag == "channel":
            cid = elm.attrib.get("id")
            names = [el.text or "" for el in elm.findall("display-name")]
            icon = elm.find("icon")
            if cid:
                chan_rows.append(
                    {
                        "epg_source_id": src_id,
                        "tvg_id": cid[:200],
                        "name": (names[0] if names else cid)[:300],
                        "icon": (icon.attrib.get("src") if icon is not None else None),
                    }
                )
            elm.clear()
        elif elm.tag == "programme":
            elm.clear()
    return root_attrs, chan_rows


def _parse_programmes(raw: bytes, *, src_id: int, wanted: set, lo, hi,
                      root_attrs: dict, timing, is_portal: bool) -> list[dict]:
    """Pass 2 as pure CPU work: programme rows for the wanted tvg_ids inside
    the bounded window. `timing` is a plain snapshot of the source's timezone
    fields, so no ORM object is ever touched from the worker thread."""
    rows: list[dict] = []
    for _event, elm in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if elm.tag == "programme":
            tvg = (elm.attrib.get("channel") or "")[:200]
            try:
                start, stop = programme_times(
                    elm.attrib, root_attrs, timing, portal=is_portal
                )
                st, en = start.utc, stop.utc
            except (ValueError, OverflowError):
                st = en = None
            if (
                tvg
                and st
                and en
                and en > st
                and tvg in wanted
                and en >= lo
                and st <= hi
            ):
                ttl = (elm.findtext("title") or "").strip()[:400]
                if ttl:
                    cat = elm.findtext("category")
                    icon = elm.find("icon")
                    rows.append(
                        {
                            "epg_source_id": src_id,
                            "tvg_id": tvg,
                            "start_ts": st,
                            "stop_ts": en,
                            "title": ttl,
                            "sub_title": (
                                (elm.findtext("sub-title") or "").strip()[:400] or None
                            ),
                            "desc": ((elm.findtext("desc") or "").strip() or None),
                            "category": (
                                ((cat or "").strip()[:200] or None) if cat else None
                            ),
                            "icon": (
                                icon.attrib.get("src") if icon is not None else None
                            ),
                        }
                    )
                    if len(rows) > 250000:
                        raise ValueError(
                            "Too many matched programmes; use a smaller guide"
                        )
            elm.clear()
        elif elm.tag == "channel":
            elm.clear()
    return rows


async def refresh_source(src_id: int, *, cached=False) -> dict:
    """Download + parse one EPG source (channels always, programmes only for
    matched tvg_ids). Returns a small report. Serialized via INGEST_LOCK."""
    async with INGEST_LOCK:
        try:
            return await _refresh_source_locked(src_id, cached=cached)
        except Exception as exc:
            PROG_BUFFER.clear()
            async with SessionLocal() as db:
                row = await db.get(EpgSource, src_id)
                if row:
                    row.status = ("failed: " + str(exc))[:120]
                    if not cached:
                        row.last_error = str(exc)[:500] or type(exc).__name__
                    await db.commit()
            await db_log(
                "ERROR", "epg", f"Source {src_id}: {type(exc).__name__}: {exc}"
            )
            return {"ok": False, "error": str(exc)}
        finally:
            PROG_BUFFER.clear()


async def _refresh_source_locked(src_id: int, *, cached=False) -> dict:
    async with SessionLocal() as s:
        src = await s.get(EpgSource, src_id)
        found = src is not None and src.enabled
        url = src.url if src else ""
    if not found:
        return {"ok": False, "error": "source not found or disabled"}

    if cached and not _cache_path(src_id, url).exists():
        return {"ok": False, "error": "No cached guide; use Refresh all"}
    if not cached and url.startswith("portal://"):
        from .runtime_settings import get_setting

        if not await get_setting("epg_portal_enabled", True):
            return {"ok": False, "error": "Portal EPG is disabled in Settings"}

    async with SessionLocal() as db:
        active = await db.get(EpgSource, src_id)
        if active:
            if not cached:
                active.last_attempt = datetime.now(timezone.utc)
            active.status = "Reprocessing cached guide…" if cached else "Refreshing…"
            await db.commit()
    note = ""
    if cached:
        raw = await asyncio.to_thread(_decode, _cache_path(src_id, url).read_bytes())
    elif url.startswith("portal://"):
        from .runtime_settings import get_setting

        if not await get_setting("epg_portal_enabled", True):
            return {"ok": False, "error": "Portal EPG is disabled in Settings"}
        from .portal_epg import portal_xml

        raw, note = await portal_xml(src.portal_id, url.removeprefix("portal://"))
    else:
        await db_log("INFO", "epg", f"downloading {url} …")
        raw = await asyncio.wait_for(_fetch(url), timeout=90)
    # ---- pass 1: channels only (must land in the DB before matching) -------
    # Both XML passes run in a worker thread: iterparse over a real guide is
    # seconds of pure CPU, and on the event loop it froze everything else.
    root_attrs, chan_rows = await asyncio.to_thread(_parse_channels, raw, src_id)
    is_portal = url.startswith("portal://")
    if (
        is_portal
        and timing_key(src)[0] != "auto"
        and root_attrs.get("spm-raw-times") != "1"
    ):
        raise ValueError(
            "This portal cache has no original timestamps; refresh the portal guide first"
        )
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(hours=78), now + timedelta(days=7)
    n_chan = len(chan_rows)

    await _upsert_channels(src_id, chan_rows)
    # A removed guide channel must not remain a selectable stale match.
    fresh = {row["tvg_id"] for row in chan_rows}
    async with SessionLocal() as db:
        old = (
            await db.execute(
                select(EpgChannel.id, EpgChannel.tvg_id).where(
                    EpgChannel.epg_source_id == src_id
                )
            )
        ).all()
        stale = [ident for ident, tvg in old if tvg not in fresh]
        for chunk in _chunked(stale):
            await db.execute(delete(EpgChannel).where(EpgChannel.id.in_(chunk)))
        await db.commit()
    if not cached:
        await asyncio.to_thread(_cache_write, src_id, url, raw)

    # ---- match BEFORE the programme pass so brand-new channel ids are in scope
    matched = await match_epg_to_playlist()
    wanted: set[str] = set()
    async with SessionLocal() as s:
        wanted.update(
            x
            for (x,) in (
                await s.execute(
                    select(LivePlaylist.epg_id).where(
                        LivePlaylist.epg_id.isnot(None), LivePlaylist.enabled.is_(True)
                    )
                )
            ).all()
            if x
        )

    async with SessionLocal() as db:
        wanted.update(
            (
                await db.scalars(
                    select(EpgChannelSource.tvg_id)
                    .join(
                        LivePlaylist,
                        EpgChannelSource.live_playlist_id == LivePlaylist.id,
                    )
                    .where(
                        EpgChannelSource.epg_source_id == src_id,
                        LivePlaylist.enabled.is_(True),
                    )
                )
            ).all()
        )

    # ---- pass 2: programmes (keep wanted ids within the bounded window only)
    # Plain-data snapshot of the timezone fields: the worker thread must not
    # read ORM attributes (a lazy refresh there would do sync IO).
    timing_snapshot = SimpleNamespace(
        timezone_mode=getattr(src, "timezone_mode", None),
        timezone_name=getattr(src, "timezone_name", None),
    )
    prog_rows = await asyncio.to_thread(
        _parse_programmes, raw, src_id=src_id, wanted=wanted, lo=lo, hi=hi,
        root_attrs=root_attrs, timing=timing_snapshot, is_portal=is_portal)
    PROG_BUFFER.extend(prog_rows)
    n_prog = len(prog_rows)

    await _flush_programmes(final=True, source_id=src_id, timing=timing_key(src))
    async with SessionLocal() as s:
        res = await s.execute(delete(EpgProgramme).where(EpgProgramme.stop_ts < lo))
        await s.commit()
        pruned = res.rowcount or 0
    msg = (
        f"{url}: {n_chan} EPG channels, {n_prog} matching programmes ingested, "
        f"{pruned} pruned, {matched} auto-matches"
    )
    async with SessionLocal() as db:
        row = await db.get(EpgSource, src_id)
        if row:
            if not cached:
                row.last_fetch = now
                row.last_error = None
            row.channel_count = n_chan
            row.status = (
                f"ok — {n_chan} channels, {n_prog} programmes"
                + (" · " + note if note else "")
            )[:120]
            await db.commit()
    await db_log("INFO", "epg", msg)
    return {
        "ok": True,
        "channels": n_chan,
        "programmes": n_prog,
        "matched": matched,
        "note": note,
    }


PROG_BUFFER: list[dict] = []
INGEST_LOCK = asyncio.Lock()  # serializes refreshes: one writer of the global buffer


async def _flush_programmes(final=False, source_id=None, timing=None):
    """Replace a complete source snapshot atomically; other feeds are untouched.

    Old/cancelled imports leave the previous guide available. A successful empty
    response removes stale events instead of falsely retaining a vanished show.
    """
    global PROG_BUFFER
    buf, PROG_BUFFER = PROG_BUFFER, []
    by_key = {
        (r["epg_source_id"], r["tvg_id"], r["start_ts"], r["title"]): r for r in buf
    }
    async with SessionLocal() as db:
        if source_id is not None:
            await db.execute(
                delete(EpgProgramme).where(EpgProgramme.epg_source_id == source_id)
            )
        for chunk in _chunked(list(by_key.values()), 200):
            await db.execute(
                _upsert_stmt(
                    EpgProgramme,
                    chunk,
                    ["epg_source_id", "tvg_id", "start_ts", "title"],
                    ["stop_ts", "sub_title", "desc", "category", "icon"],
                )
            )
        if timing is not None and source_id is not None:
            source = await db.get(EpgSource, source_id)
            if source:
                source.applied_timezone_mode, source.applied_timezone_name = timing
        await db.commit()


async def refresh_all() -> dict:
    await ensure_portal_sources()
    async with SessionLocal() as s:
        ids = [
            x.id
            for x in (
                await s.execute(select(EpgSource).where(EpgSource.enabled.is_(True)))
            )
            .scalars()
            .all()
        ]
    out = {"sources": len(ids), "results": []}
    for i in ids:
        out["results"].append(await refresh_source(i))
    return out


async def reingest_cached_sources():
    async with SessionLocal() as db:
        rows = (
            await db.scalars(select(EpgSource).where(EpgSource.enabled.is_(True)))
        ).all()
    for source in rows:
        if _cache_path(source.id, source.url).exists():
            await refresh_source(source.id, cached=True)


async def reindex_source_snapshots():
    """Recover independent mirror data once from pre-upgrade caches, offline."""
    from .runtime_settings import get_setting

    if await get_setting("epg_source_snapshots_v1", False):
        return
    # Preserve the previous scheduler's attempt timestamps and visible errors
    # before cached reprocessing changes its progress/status text.
    async with SessionLocal() as db:
        for source in (await db.scalars(select(EpgSource))).all():
            if source.last_attempt is None:
                attempt = await db.get(
                    Setting, f"epg_attempt_{_source_key(source.url)}"
                )
                if attempt:
                    try:
                        stamp = float(json.loads(attempt.value))
                        if stamp > 0:
                            source.last_attempt = datetime.fromtimestamp(
                                stamp, timezone.utc
                            )
                    except (TypeError, ValueError, OverflowError, OSError):
                        pass
            if not source.last_error and (source.status or "").startswith("failed:"):
                source.last_error = source.status.removeprefix("failed:").strip()
        await db.commit()
    await reingest_cached_sources()
    async with SessionLocal() as db:
        marker = await db.get(Setting, "epg_source_snapshots_v1")
        if marker is None:
            db.add(Setting(key="epg_source_snapshots_v1", value="true"))
        else:
            marker.value = "true"
        await db.commit()


async def build_xmltv(base_url: str, user) -> str:
    """Merge the user's visible live channels with EPG programmes."""
    from ..services.playlist_gen import _allowed, _groups  # reuse user filters
    from .user_groups import group_name

    groups = _groups(user)
    now = datetime.now(timezone.utc)
    async with SessionLocal() as s:
        items = (
            (
                await s.execute(
                    select(LivePlaylist)
                    .where(LivePlaylist.enabled.is_(True))
                    .order_by(LivePlaylist.order, LivePlaylist.id)
                )
            )
            .scalars()
            .all()
        )
        chans = []
        for it in items:
            if not _allowed(group_name("live", it.group_name), groups["live"]):
                continue
            cid = channel_epg_id(it)
            chans.append((it, cid))
        from .epg_policy import load_schedules

        schedules, _, _, _ = await load_schedules(s, [item for item, _ in chans], now)

    def esc(t: str | None) -> str:
        from xml.sax.saxutils import escape as _e

        return _e(t or "", {'"': "&quot;", "'": "&apos;"})

    L = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        '<tv generator-info-name="stalker-proxy-manager">',
    ]
    emitted = set()
    for it, cid in chans:
        if cid in emitted:
            continue
        emitted.add(cid)
        L.append(f'  <channel id="{esc(cid)}">')
        L.append(f"    <display-name>{esc(it.custom_name)}</display-name>")
        if it.logo:
            logo = it.logo if it.logo.startswith("http") else f"{base_url}{it.logo}"
            L.append(f'    <icon src="{esc(logo)}"/>')
        L.append("  </channel>")
    emitted_programmes = set()
    for item, cid in chans:
        if cid in emitted_programmes:
            continue
        emitted_programmes.add(cid)
        for event in schedules.get(item.id, []):
            p = event.programme
            L.append(
                f'  <programme start="{_fmt_ts(event.start)}" stop="{_fmt_ts(event.stop)}" channel="{esc(cid)}">'
            )
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


def channel_epg_id(item) -> str:
    if (
        getattr(item, "epg_custom", False)
        or getattr(item, "epg_has_mappings", False)
        or getattr(item, "epg_offset_minutes", 0)
    ):
        return f"spm.live.{item.id}"
    return (item.epg_id or "").strip() or f"spm.live.{item.id}"


# ------------------------------- scheduler ----------------------------------
async def schedule_info():
    from .runtime_settings import get_setting

    try:
        hours = int(await get_setting("epg_refresh_hours", 24))
        hours = min(168, max(0, hours))
    except (TypeError, ValueError):
        hours = 24
    return {
        "hours": hours,
        "enabled": hours > 0,
        "portal_enabled": bool(await get_setting("epg_portal_enabled", True)),
    }


async def refresh_due():
    schedule = await schedule_info()
    if not schedule["enabled"]:
        return []
    await ensure_portal_sources()
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        rows = (
            (await db.execute(select(EpgSource).where(EpgSource.enabled.is_(True))))
            .scalars()
            .all()
        )
        active_portals = set(
            (await db.scalars(select(Portal.id).where(Portal.enabled.is_(True)))).all()
        )
    results = []
    from .epg_policy import effective_hours, aware

    for row in rows:
        hours = effective_hours(row, schedule["hours"])
        if not hours:
            continue
        if row.url.startswith("portal://") and (
            not schedule["portal_enabled"] or row.portal_id not in active_portals
        ):
            continue
        # Persist attempt time separately: a dead mirror should not be retried
        # every scheduler tick. Sources are still independent of each other.
        from .runtime_settings import get_setting

        attempted = await get_setting(f"epg_attempt_{_source_key(row.url)}", 0)
        last = (
            row.last_fetch.timestamp()
            if row.last_fetch and row.last_fetch.tzinfo
            else (
                row.last_fetch.replace(tzinfo=timezone.utc).timestamp()
                if row.last_fetch
                else 0
            )
        )
        try:
            due = (
                now.timestamp()
                - max(
                    last,
                    float(attempted or 0),
                    aware(row.last_attempt).timestamp() if row.last_attempt else 0,
                )
                >= hours * 3600
            )
        except (TypeError, ValueError):
            due = True
        if not due:
            continue
        if INGEST_LOCK.locked():
            break
        async with SessionLocal() as db:
            current = await db.get(EpgSource, row.id)
            if current:
                current.last_attempt = now
            key = f"epg_attempt_{_source_key(row.url)}"
            setting = await db.get(Setting, key)
            if setting is None:
                db.add(Setting(key=key, value=json.dumps(now.timestamp())))
            else:
                setting.value = json.dumps(now.timestamp())
            await db.commit()
        results.append(await refresh_source(row.id))
    return results


async def epg_scheduler(parse=60) -> None:
    """Re-read settings every minute; zero pauses automatic (not manual) refresh."""
    while True:
        await asyncio.sleep(parse)
        try:
            await refresh_due()
        except Exception as exc:
            await db_log("ERROR", "epg", f"scheduler tick failed: {exc}")
