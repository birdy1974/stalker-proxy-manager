"""
Background fetch jobs: genres -> source items -> series seasons/episodes.

Why chunked background jobs: real portals are slow (~1s/page, 14 items/page -
see Phase-1 notes) and catalogs can be 60k+ items. The HTTP request that
starts a fetch returns immediately; the job updates:
  * its public progress state (polled by the GUI every 1.5s)
  * the logs table (milestones INFO, per-page DEBUG)
  * per-genre item counts + `*_fetched` flags (dashboard "X of Y")

Only ENABLED genres are fetched (spec workflow step 4/6). Page budget per
genre comes from FETCH_PAGE_BUDGET so one giant genre cannot stall the queue;
a "fetch more" button re-runs the job which resumes (fetched flag per genre).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from ..config import FETCH_PAGE_BUDGET
from ..database import SessionLocal
from ..models import (
    LiveGenre, LiveSource, MacAddress, Portal, SerieEpisode, SerieGenre,
    SerieSeason, SerieSource, VodGenre, VodSource,
)
from ..portal.client import PortalError, StalkerClient
from ..portal.resolver import resolve_portal
from .db_logging import db_log


@dataclass
class Job:
    id: str
    kind: str
    portal_id: int
    status: str = "queued"                         # queued|running|done|error|cancelled
    stage: str = "queued"
    detail: str = ""
    done_items: int = 0
    total_items: int = 0
    started: float = 0.0
    ended: float = 0.0
    error: str = ""
    _cancel: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "portal_id": self.portal_id,
                "status": self.status, "stage": self.stage, "detail": self.detail,
                "done_items": self.done_items, "total_items": self.total_items,
                "started": self.started, "ended": self.ended, "error": self.error}


# How many series are handled per database session/transaction. Bounds memory
# and transaction size while keeping the grouped access (one session, one SELECT
# per table per chunk) instead of one round trip per row.
SERIES_CHUNK = 25

JOBS: dict[str, Job] = {}
_QUEUE: asyncio.Queue[Job] = asyncio.Queue()
_WORKER_STARTED = False


def get_job(job_id: str) -> Job | None:
    return JOBS.get(job_id)


def list_jobs() -> list[dict]:
    return [j.public() for j in sorted(JOBS.values(), key=lambda j: j.started, reverse=True)[:50]]


async def submit(kind: str, portal_id: int) -> Job:
    for j in JOBS.values():                                       # one job per portal
        if j.portal_id == portal_id and j.status in ("queued", "running"):
            return j
    job = Job(id=uuid.uuid4().hex[:12], kind=kind, portal_id=portal_id)
    JOBS[job.id] = job
    await _QUEUE.put(job)
    _ensure_worker()
    return job


def cancel(job_id: str) -> bool:
    job = JOBS.get(job_id)
    if job:
        job._cancel.set()
        return True
    return False


def _ensure_worker() -> None:
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        _WORKER_STARTED = True
        asyncio.get_running_loop().create_task(_worker())


async def _worker() -> None:
    while True:
        job = await _QUEUE.get()
        if job.status == "cancelled":
            continue
        job.status, job.started, job.stage = "running", time.time(), "starting"
        try:
            if job.kind == "fetch_portal":
                await _run_portal_fetch(job)
            job.status = "cancelled" if job._cancel.is_set() else "done"
        except Exception as exc:  # noqa: BLE001
            job.status, job.error = "error", f"{type(exc).__name__}: {exc}"
            await db_log("ERROR", "fetch", f"portal {job.portal_id}: job failed: {job.error}")
        finally:
            job.ended = time.time()
            job.stage = job.status


# --------------------------------------------------------------------------- #
async def _run_portal_fetch(job: Job) -> None:
    async with SessionLocal() as s:
        portal = await s.get(Portal, job.portal_id)
        if not portal:
            raise PortalError("portal not found")
        portal_name, base, resolved = portal.name, portal.base_url, portal.resolved_url
        macs = (await s.execute(
            select(MacAddress).where(MacAddress.portal_id == portal.id)
            .order_by(MacAddress.order))).scalars().all()
    if not macs:
        raise PortalError("portal has no MAC addresses")

    mac = macs[0]
    # ---- resolve once per job if needed ------------------------------------
    if not resolved:
        job.stage = "resolving portal url"
        await db_log("INFO", "fetch", f"[{portal_name}] resolving portal path for {base}")
        res = await resolve_portal(base, mac=mac.mac)
        for line in res.attempts:
            await db_log("DEBUG", "resolve", f"[{portal_name}] {line}")
        if not res.ok:
            raise PortalError(f"resolve failed: {res.error}")
        resolved = res.portal_url
        async with SessionLocal() as s:
            p = await s.get(Portal, job.portal_id)
            p.resolved_url, p.resolved_path = res.portal_url, res.path
            await s.commit()
        await db_log("INFO", "fetch", f"[{portal_name}] resolved -> {resolved}")

    client = StalkerClient(resolved, mac.mac, mac.password)
    try:
        await client.handshake()
        # refresh MAC expiry/status while we are here
        try:
            exp = await client.account_expires()
            async with SessionLocal() as s:
                m = await s.get(MacAddress, mac.id)
                m.expire_date, m.status, m.online = exp, "online", True
                m.last_checked = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                await s.commit()
        except PortalError:
            pass

        await _fetch_live(job, client, job.portal_id, portal_name)
        if job._cancel.is_set():
            return
        await _fetch_vod(job, client, job.portal_id, portal_name)
        if job._cancel.is_set():
            return
        await _fetch_series(job, client, job.portal_id, portal_name)
        # Seasons arrive after the series were added to the playlist, so their
        # link rows have to be reconciled here - otherwise the series stay in
        # the builder but contribute no episodes to any playlist.
        try:
            from .playlist_sync import sync_season_links
            async with SessionLocal() as s:
                added = await sync_season_links(s)
            if added:
                await db_log("INFO", "fetch",
                             f"[{portal_name}] linked {added} new season(s) to playlist series")
        except Exception:  # noqa: BLE001 - never fail a fetch over bookkeeping
            log.exception("season link sync failed")
        await db_log("INFO", "fetch", f"[{portal_name}] fetch finished: {job.done_items} items")
    finally:
        await client.close()


async def _sync_genres(s, model, portal_id: int, incoming: list[dict], syn_ok_none=True) -> dict[str, int]:
    """Upsert genres (preserve enabled), return {genre_portal_id: row.id}."""
    known: dict[str, int] = {}
    for g in incoming:
        gid = str(g.get("id", "")).strip()
        if not gid or gid == "*":
            continue
        title = (g.get("title") or g.get("name") or gid)[:300]
        row = (await s.execute(select(model).where(
            model.portal_id == portal_id, model.genre_portal_id == gid))).scalar_one_or_none()
        if row is None:
            row = model(portal_id=portal_id, genre_portal_id=gid, name=title, enabled=False)
            s.add(row)
            await s.flush()
        else:
            row.name = title
        known[gid] = row.id
    return known


async def _paged_upsert(job, fetch_page, upsert_many, genre_name,
                        portal_name, budget=FETCH_PAGE_BUDGET) -> tuple[int, int]:
    """
    Fetch up to `budget` pages for one genre and hand each page to `upsert_many`
    as a WHOLE (one SELECT + one COMMIT per page instead of one round trip per
    item). Returns (n, total).
    """
    inserted, total = 0, 0
    for page in range(1, budget + 1):
        if job._cancel.is_set():
            break
        job.detail = f"{genre_name}: page {page}"
        data = await fetch_page(page)
        if page == 1:
            total = data.total
        if data.items:
            await upsert_many(data.items)
            inserted += len(data.items)
        job.done_items += len(data.items)
        if len(data.items) < 14:                      # short page = end of list
            break
        if total and page * 14 >= total:
            break
    await db_log("INFO", "fetch",
                 f"[{portal_name}] {genre_name}: {inserted} items (portal total: {total or '?'})")
    return inserted, total


# --------------------------------------------------------------------------- #
# Grouped database access: one SELECT for the whole page, then in-memory
# update/insert and ONE commit. (Fetching item by item is what the portal API
# costs us anyway - the database must not multiply it.)
# --------------------------------------------------------------------------- #
def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


async def _bulk_upsert(s, model, portal_id: int, key_col, items: list[dict],
                       key_of, apply_fields, chunk_size: int = 500) -> int:
    """
    Upsert `items` with two round trips per chunk (SELECT existing + COMMIT)
    instead of one session per row.
    """
    done = 0
    for chunk in _chunks(items, chunk_size):
        keys, rows_by_key = [], {}
        for item in chunk:
            key = key_of(item)
            if key:
                keys.append(key)
                rows_by_key.setdefault(key, item)          # first wins on dupes
        if not keys:
            continue
        existing = {getattr(r, key_col.key): r for r in (await s.execute(
            select(model).where(model.portal_id == portal_id,
                                key_col.in_(keys)))).scalars().all()}
        for key, item in rows_by_key.items():
            row = existing.get(key)
            if row is None:
                row = model(portal_id=portal_id, **{key_col.key: key})
                s.add(row)
            apply_fields(row, item)
            done += 1
        await s.commit()
    return done


def _season_number(item: dict) -> int:
    """Season number of a season row; panels use season_id, 'Season 3' or nothing."""
    raw = str(item.get("season_id") or item.get("name") or "1")
    raw = raw.replace("Season", "").replace("season", "").strip()
    try:
        return max(int(raw), 1)
    except ValueError:
        return 1


def _episode_number(item: dict) -> int | None:
    """Episode number from the `series` pair [season, episode] if the panel sends it."""
    meta = item.get("series") or []
    if len(meta) > 1:
        try:
            return int(meta[1])
        except (TypeError, ValueError):
            return None
    return None


def _episode_season(item: dict) -> int | None:
    """
    Season number carried by an episode row - ONLY trusted when the row really
    is an episode, i.e. it carries the full `series: [season, episode]` pair.
    Season rows (and season-1-only answers) carry no such pair and must never
    be mistaken for the complete episode list.
    """
    meta = item.get("series") or []
    if len(meta) > 1:
        try:
            return int(meta[0])
        except (TypeError, ValueError):
            return None
    return None


def _live_genre_of(item: dict) -> str:
    """Portal genre id of a channel row (field name differs between panels)."""
    for key in ("tv_genre_id", "genre_id", "genre", "tv_genre"):
        val = item.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _live_fields(row, item, genre_db_id: int) -> None:
    row.live_genre_id = genre_db_id
    row.original_name = (item.get("name") or "?")[:300]
    row.number = str(item.get("number", "") or "")[:20]
    row.cmd = item.get("cmd")
    row.logo_original = (item.get("logo") or "")[:600] or None
    row.epg_original = (item.get("epg_channel_id") or item.get("tvg_id") or "")[:200] or None
    row.tv_archive = str(item.get("tv_archive", "0")) == "1"


def _vod_fields(row, item, genre_db_id: int) -> None:
    row.vod_genre_id = genre_db_id
    row.original_name = (item.get("name") or item.get("o_name") or "?")[:400]
    row.cmd = item.get("cmd")
    row.position = item.get("position") if isinstance(item.get("position"), int) else None
    row.poster = (item.get("screenshot_uri") or "")[:600] or None
    row.year = str(item.get("year", "") or "")[:10]
    row.description = item.get("description")
    row.genre = (item.get("genre") or item.get("category_name") or "")[:300] or None
    row.director = (item.get("director") or "")[:300] or None
    row.actors = item.get("actors")
    row.rating = str(item.get("rating_imdb", "") or item.get("rating_kinopoisk", "") or "")[:10]
    row.duration = str(item.get("time", "") or "")[:20]
    row.added = str(item.get("added", "") or "")[:40]


def _serie_fields(row, item, genre_db_id: int) -> None:
    row.serie_genre_id = genre_db_id
    row.original_name = (item.get("name") or item.get("o_name") or "?")[:400]
    row.poster = (item.get("screenshot_uri") or "")[:600] or None
    row.year = str(item.get("year", "") or "")[:10]
    row.description = item.get("description")
    row.rating = str(item.get("rating_kinopoisk", "") or "")[:10]
    row.category_name = (item.get("category_name") or "")[:300] or None


async def _fetch_live(job: Job, client: StalkerClient, portal_id: int, portal_name: str) -> None:
    job.stage = "live genres"
    genres = await client.live_genres()
    async with SessionLocal() as s:
        id_map = await _sync_genres(s, LiveGenre, portal_id, genres)
        await s.commit()
        enabled = (await s.execute(select(LiveGenre).where(
            LiveGenre.portal_id == portal_id, LiveGenre.enabled.is_(True)))).scalars().all()
        enabled = [(g.id, g.genre_portal_id, g.name) for g in enabled]

    async def upsert_many(items, db_id: int) -> int:
        async with SessionLocal() as s2:
            return await _bulk_upsert(
                s2, LiveSource, portal_id, LiveSource.portal_channel_id, items,
                lambda i: str(i.get("id", "") or ""),
                lambda row, item: _live_fields(row, item, db_id))

    # ---- fast path: the COMPLETE channel list in a single request ----------
    # Real STBs ask for `get_all_channels` instead of paging through every
    # genre; when the portal answers we store the whole catalog at once and
    # only split it into the enabled genres locally.
    complete: list[dict] = []
    try:
        complete = await client.all_channels()
    except PortalError as exc:
        await db_log("DEBUG", "fetch",
                     f"[{portal_name}] get_all_channels unavailable ({exc}) - paging per genre")
    if complete:
        job.stage = "live channels"
        job.detail = f"live: complete list ({len(complete)} rows)"
        counts: dict[str, int] = {}
        bucket: dict[str, list[dict]] = {}
        for row in complete:
            gid = _live_genre_of(row)
            counts[gid] = counts.get(gid, 0) + 1
            bucket.setdefault(gid, []).append(row)
        total_rows = len(complete)
        for db_id, gid, name in enabled:
            if job._cancel.is_set():
                return
            rows = bucket.get(str(gid or ""), [])
            job.detail = f"live:{name} ({len(rows)} of {total_rows})"
            if rows:
                await upsert_many(rows, db_id)
                job.done_items += len(rows)
        async with SessionLocal() as s3:                 # ONE commit for all counters
            for db_id, gid, _name in enabled:
                g = await s3.get(LiveGenre, db_id)
                g.item_count, g.channels_fetched = counts.get(str(gid or ""), 0), True
            await s3.commit()
        await db_log("INFO", "fetch",
                     f"[{portal_name}] live: {total_rows} channels via get_all_channels "
                     f"(1 request, {len(enabled)} enabled genres)")
        return

    # ---- fallback: page through every enabled genre ------------------------
    for db_id, gid, name in enabled:
        if job._cancel.is_set():
            return
        job.stage = "live channels"

        async def page_fetch(p, _gid=gid):  # noqa: ANN202
            return await client.live_channels(_gid, p)

        n, total = await _paged_upsert(job, page_fetch,
                                       lambda items, _db_id=db_id: upsert_many(items, _db_id),
                                       f"live:{name}", portal_name)
        async with SessionLocal() as s3:
            g = await s3.get(LiveGenre, db_id)
            g.item_count, g.channels_fetched = (total or n), True
            await s3.commit()


async def _fetch_vod(job: Job, client: StalkerClient, portal_id: int, portal_name: str) -> None:
    job.stage = "vod genres"
    cats = await client.vod_genres()
    async with SessionLocal() as s:
        id_map = await _sync_genres(s, VodGenre, portal_id, cats)
        if not id_map:                                # no categories -> synthetic "All"
            row = (await s.execute(select(VodGenre).where(
                VodGenre.portal_id == portal_id, VodGenre.genre_portal_id == ""))).scalar_one_or_none()
            if row is None:
                row = VodGenre(portal_id=portal_id, genre_portal_id="", name="(All VOD)", enabled=True)
                s.add(row)
        await s.commit()
        enabled = (await s.execute(select(VodGenre).where(
            VodGenre.portal_id == portal_id, VodGenre.enabled.is_(True)))).scalars().all()
        enabled = [(g.id, g.genre_portal_id, g.name) for g in enabled]

    async def upsert_many(items, db_id: int) -> int:
        async with SessionLocal() as s2:
            return await _bulk_upsert(
                s2, VodSource, portal_id, VodSource.portal_item_id, items,
                lambda i: str(i.get("id", "") or ""),
                lambda row, item: _vod_fields(row, item, db_id))

    for db_id, cid, name in enabled:
        if job._cancel.is_set():
            return
        job.stage = "vod items"

        async def page_fetch(p, _cid=cid):
            return await client.vod_list(_cid or None, p)

        n, total = await _paged_upsert(job, page_fetch,
                                       lambda items, _db_id=db_id: upsert_many(items, _db_id),
                                       f"vod:{name}", portal_name)
        async with SessionLocal() as s3:
            g = await s3.get(VodGenre, db_id)
            g.item_count, g.items_fetched = (total or n), True
            await s3.commit()


async def _fetch_series(job: Job, client: StalkerClient, portal_id: int, portal_name: str) -> None:
    job.stage = "series genres"
    cats = await client.series_genres()
    async with SessionLocal() as s:
        id_map = await _sync_genres(s, SerieGenre, portal_id, cats)
        if not id_map:
            row = (await s.execute(select(SerieGenre).where(
                SerieGenre.portal_id == portal_id, SerieGenre.genre_portal_id == ""))).scalar_one_or_none()
            if row is None:
                row = SerieGenre(portal_id=portal_id, genre_portal_id="", name="(All series)", enabled=True)
                s.add(row)
        await s.commit()
        enabled = (await s.execute(select(SerieGenre).where(
            SerieGenre.portal_id == portal_id, SerieGenre.enabled.is_(True)))).scalars().all()
        enabled = [(g.id, g.genre_portal_id, g.name) for g in enabled]

    async def upsert_many(items, db_id: int) -> int:
        async with SessionLocal() as s2:
            return await _bulk_upsert(
                s2, SerieSource, portal_id, SerieSource.portal_item_id, items,
                lambda i: str(i.get("id", "") or ""),
                lambda row, item: _serie_fields(row, item, db_id))

    for db_id, cid, name in enabled:
        if job._cancel.is_set():
            return
        job.stage = "series items"

        async def page_fetch(p, _cid=cid):
            return await client.series_list(_cid or None, p)

        n, total = await _paged_upsert(job, page_fetch,
                                       lambda items, _db_id=db_id: upsert_many(items, _db_id),
                                       f"series:{name}", portal_name)
        async with SessionLocal() as s3:
            g = await s3.get(SerieGenre, db_id)
            g.item_count, g.items_fetched = (total or n), True
            await s3.commit()

    # ---- seasons + episodes for enabled series (Q6=A: season-level) --------
    # The portal has no "all seasons of all series" call, so one request per
    # series is unavoidable - but the DATABASE side is grouped: one SELECT for
    # all season rows of a chunk, one SELECT for all episodes of a series and
    # one COMMIT per series instead of a round trip per row.
    job.stage = "series seasons/episodes"
    async with SessionLocal() as s:
        series = (await s.execute(select(SerieSource).where(
            SerieSource.portal_id == portal_id, SerieSource.enabled.is_(True),
            SerieSource.seasons_fetched.is_(False)))).scalars().all()
        ids = [(x.id, x.portal_item_id, x.original_name) for x in series]
    total_series = len(ids)
    whole_episode_list = True          # hope for "all episodes in one request"
    done_series = 0
    for chunk in _chunks(ids, SERIES_CHUNK):
        if job._cancel.is_set():
            return
        async with SessionLocal() as s2:
            # ONE query for every season row of the whole chunk
            season_rows = (await s2.execute(select(SerieSeason).where(
                SerieSeason.serie_source_id.in_([c[0] for c in chunk])))).scalars().all()
            by_series: dict[int, dict[int, SerieSeason]] = {}
            for r in season_rows:
                by_series.setdefault(r.serie_source_id, {})[r.season_number] = r

            for sid, pid, sname in chunk:
                if job._cancel.is_set():
                    return
                done_series += 1
                job.detail = f"series {done_series}/{total_series}: {sname}"
                try:
                    seasons = await client.series_seasons(pid)
                    wanted: list[tuple[int, str, str]] = []
                    for sitem in seasons:
                        snum = _season_number(sitem)
                        wanted.append((snum, str(sitem.get("season_id") or snum),
                                       (sitem.get("name") or f"Season {snum}")[:300]))
                    # --- complete episode list in ONE request when supported
                    bulk: dict[int, list[dict]] = {}
                    if whole_episode_list and len(wanted) > 1:
                        try:
                            for e in await client.series_episodes(pid, None):
                                sn = _episode_season(e)
                                if sn is not None:
                                    bulk.setdefault(sn, []).append(e)
                        except PortalError:
                            pass
                        if len(bulk) < 2:
                            # not a real "all episodes" answer (single season,
                            # or the panel returned seasons again) -> per season
                            bulk = {}
                            whole_episode_list = False       # stop asking

                    rows: dict[int, tuple[SerieSeason, str]] = {}
                    for snum, pseason, stitle in wanted:
                        row = by_series.setdefault(sid, {}).get(snum)
                        if row is None:
                            row = SerieSeason(serie_source_id=sid, season_number=snum, enabled=True)
                            s2.add(row)
                            await s2.flush()                 # need row.id for episodes
                            by_series[sid][snum] = row
                        row.portal_season_id = pseason
                        row.name = stitle
                        rows[snum] = (row, pseason)

                    # ONE query for all episodes of all seasons of this series
                    existing_eps: dict[int, dict[int, SerieEpisode]] = {}
                    season_ids = [r.id for r, _ in rows.values()]
                    if season_ids:
                        q = select(SerieEpisode).where(SerieEpisode.serie_season_id.in_(season_ids))
                        for e in (await s2.execute(q)).scalars().all():
                            existing_eps.setdefault(e.serie_season_id, {})[e.episode_number] = e

                    for snum, (row, pseason) in rows.items():
                        if row.episodes_fetched:
                            continue
                        eps = bulk.get(snum)
                        if eps is None:
                            eps = await client.series_episodes(pid, pseason)
                        cursor = max(existing_eps.get(row.id, {}), default=0)
                        for e in eps:
                            en = _episode_number(e)
                            if en is None:                   # fall back to running order
                                cursor += 1
                                en = cursor
                            erow = existing_eps.setdefault(row.id, {}).get(en)
                            if erow is None:
                                erow = SerieEpisode(serie_season_id=row.id, episode_number=en)
                                s2.add(erow)
                                existing_eps[row.id][en] = erow
                            erow.portal_item_id = str(e.get("id", ""))[:60]
                            erow.name = (e.get("name") or f"E{en:02d}")[:400]
                            erow.cmd = e.get("cmd")
                            erow.duration = str(e.get("time", "") or "")[:20] or None
                        row.episodes_fetched = True

                    srow = await s2.get(SerieSource, sid)
                    srow.seasons_fetched = True
                    await s2.commit()
                except PortalError as exc:
                    await s2.rollback()
                    await db_log("WARNING", "fetch", f"[{portal_name}] series '{sname}': {exc}")
                job.done_items += 1
