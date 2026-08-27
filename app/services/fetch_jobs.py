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

from sqlalchemy import func, select

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


async def _paged_upsert(job, s, fetch_page, upsert_one, genre_db_id, genre_name,
                        portal_name, budget=FETCH_PAGE_BUDGET) -> tuple[int, int]:
    """Fetch up to `budget` pages for one genre; upsert each item. Returns (n, total)."""
    inserted, total = 0, 0
    for page in range(1, budget + 1):
        if job._cancel.is_set():
            break
        job.detail = f"{genre_name}: page {page}"
        data = await fetch_page(page)
        if page == 1:
            total = data.total
        for item in data.items:
            await upsert_one(item)
            inserted += 1
        job.done_items += len(data.items)
        if len(data.items) < 14:                      # short page = end of list
            break
        if total and page * 14 >= total:
            break
    await db_log("INFO", "fetch",
                 f"[{portal_name}] {genre_name}: {inserted} items (portal total: {total or '?'})")
    return inserted, total


async def _fetch_live(job: Job, client: StalkerClient, portal_id: int, portal_name: str) -> None:
    job.stage = "live genres"
    genres = await client.live_genres()
    async with SessionLocal() as s:
        id_map = await _sync_genres(s, LiveGenre, portal_id, genres)
        await s.commit()
        enabled = (await s.execute(select(LiveGenre).where(
            LiveGenre.portal_id == portal_id, LiveGenre.enabled.is_(True)))).scalars().all()
        enabled = [(g.id, g.genre_portal_id, g.name) for g in enabled]
    for db_id, gid, name in enabled:
        if job._cancel.is_set():
            return
        job.stage = "live channels"

        async def upsert(item, _db_id=db_id, _gid=gid):  # noqa: ANN202
            pid = str(item.get("id", ""))
            if not pid:
                return
            async with SessionLocal() as s2:
                row = (await s2.execute(select(LiveSource).where(
                    LiveSource.portal_id == portal_id, LiveSource.portal_channel_id == pid)
                )).scalar_one_or_none()
                if row is None:
                    row = LiveSource(portal_id=portal_id, portal_channel_id=pid, original_name="?")
                    s2.add(row)
                row.live_genre_id = _db_id
                row.original_name = (item.get("name") or "?")[:300]
                row.number = str(item.get("number", "") or "")[:20]
                row.cmd = item.get("cmd")
                row.logo_original = (item.get("logo") or "")[:600] or None
                row.epg_original = (item.get("epg_channel_id") or item.get("tvg_id") or "")[:200] or None
                row.tv_archive = str(item.get("tv_archive", "0")) == "1"
                await s2.commit()

        async def page_fetch(p):  # noqa: ANN202
            return await client.live_channels(gid, p)

        n, total = await _paged_upsert(job, None, page_fetch, upsert, db_id, f"live:{name}", portal_name)
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

    for db_id, cid, name in enabled:
        if job._cancel.is_set():
            return
        job.stage = "vod items"

        async def upsert(item, _db_id=db_id):
            pid = str(item.get("id", ""))
            if not pid:
                return
            async with SessionLocal() as s2:
                row = (await s2.execute(select(VodSource).where(
                    VodSource.portal_id == portal_id, VodSource.portal_item_id == pid)
                )).scalar_one_or_none()
                if row is None:
                    row = VodSource(portal_id=portal_id, portal_item_id=pid, original_name="?")
                    s2.add(row)
                row.vod_genre_id = _db_id
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
                await s2.commit()

        async def page_fetch(p):
            return await client.vod_list(cid or None, p)

        n, total = await _paged_upsert(job, None, page_fetch, upsert, db_id, f"vod:{name}", portal_name)
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

    for db_id, cid, name in enabled:
        if job._cancel.is_set():
            return
        job.stage = "series items"

        async def upsert(item, _db_id=db_id):
            pid = str(item.get("id", ""))
            if not pid:
                return
            async with SessionLocal() as s2:
                row = (await s2.execute(select(SerieSource).where(
                    SerieSource.portal_id == portal_id, SerieSource.portal_item_id == pid)
                )).scalar_one_or_none()
                if row is None:
                    row = SerieSource(portal_id=portal_id, portal_item_id=pid, original_name="?")
                    s2.add(row)
                row.serie_genre_id = _db_id
                row.original_name = (item.get("name") or item.get("o_name") or "?")[:400]
                row.poster = (item.get("screenshot_uri") or "")[:600] or None
                row.year = str(item.get("year", "") or "")[:10]
                row.description = item.get("description")
                row.rating = str(item.get("rating_kinopoisk", "") or "")[:10]
                row.category_name = (item.get("category_name") or "")[:300] or None
                await s2.commit()

        async def page_fetch(p):
            return await client.series_list(cid or None, p)

        n, total = await _paged_upsert(job, None, page_fetch, upsert, db_id, f"series:{name}", portal_name)
        async with SessionLocal() as s3:
            g = await s3.get(SerieGenre, db_id)
            g.item_count, g.items_fetched = (total or n), True
            await s3.commit()

    # ---- seasons + episodes for enabled series (Q6=A: season-level) --------
    job.stage = "series seasons/episodes"
    async with SessionLocal() as s:
        series = (await s.execute(select(SerieSource).where(
            SerieSource.portal_id == portal_id, SerieSource.enabled.is_(True),
            SerieSource.seasons_fetched.is_(False)))).scalars().all()
        ids = [(x.id, x.portal_item_id, x.original_name) for x in series]
    total_series = len(ids)
    for idx, (sid, pid, sname) in enumerate(ids, 1):
        if job._cancel.is_set():
            return
        job.detail = f"series {idx}/{total_series}: {sname}"
        try:
            seasons = await client.series_seasons(pid)
            async with SessionLocal() as s2:
                for sitem in seasons:
                    snum = int(str(sitem.get("season_id") or sitem.get("name", "1")).replace("Season", "").strip() or 1)
                    snum = max(snum, 1)
                    row = (await s2.execute(select(SerieSeason).where(
                        SerieSeason.serie_source_id == sid, SerieSeason.season_number == snum)
                    )).scalar_one_or_none()
                    if row is None:
                        row = SerieSeason(serie_source_id=sid, season_number=snum, enabled=True)
                        s2.add(row)
                        await s2.flush()
                    row.portal_season_id = str(sitem.get("season_id") or snum)
                    row.name = (sitem.get("name") or f"Season {snum}")[:300]
                    if row.episodes_fetched:
                        continue
                    eps = await client.series_episodes(pid, str(sitem.get("season_id") or ""))
                    for e in eps:
                        series_meta = e.get("series") or []
                        epnum = epnum = int(series_meta[1]) if len(series_meta) > 1 else None
                        if epnum is None:
                            # fall back to running order
                            epnum = (await s2.execute(select(func.count()).select_from(SerieEpisode).where(
                                SerieEpisode.serie_season_id == row.id))).scalar() + 1
                        erow = (await s2.execute(select(SerieEpisode).where(
                            SerieEpisode.serie_season_id == row.id,
                            SerieEpisode.episode_number == epnum))).scalar_one_or_none()
                        if erow is None:
                            erow = SerieEpisode(serie_season_id=row.id, episode_number=epnum)
                            s2.add(erow)
                        erow.portal_item_id = str(e.get("id", ""))[:60]
                        erow.name = (e.get("name") or f"E{epnum:02d}")[:400]
                        erow.cmd = e.get("cmd")
                        erow.duration = str(e.get("time", "") or "")[:20] or None
                    row.episodes_fetched = True
                srow = await s2.get(SerieSource, sid)
                srow.seasons_fetched = True
                await s2.commit()
        except PortalError as exc:
            await db_log("WARNING", "fetch", f"[{portal_name}] series '{sname}': {exc}")
        job.done_items += 1
