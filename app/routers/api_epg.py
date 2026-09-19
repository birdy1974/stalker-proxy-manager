"""
EPG API (Phase 3): EPG source CRUD + refresh + auto-match controls.
Backend for the Settings page EPG section.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool, StrictInt, ValidationError
from sqlalchemy import func, select

from ..database import get_db
from ..models import (
    EpgChannel,
    EpgProgramme,
    EpgSource,
    LivePlaylist,
    Portal,
    EpgChannelSource,
)
from ..security import require_admin
from ..services import epg as epg_svc
from ..services.db_logging import db_log
from ..services.epg_policy import ChannelGuide
from ..services.epg_timing import SourceTiming, TimezoneMode, timing_key, timing_pending

router = APIRouter(
    prefix="/api/epg", tags=["epg"], dependencies=[Depends(require_admin)]
)

_refresh_task: asyncio.Task | None = None
_cache_task: asyncio.Task | None = None
_cache_pending = False


def queue_cached_refresh():
    global _cache_task, _cache_pending
    _cache_pending = True
    if _cache_task and not _cache_task.done():
        return

    async def run():
        global _cache_pending
        await asyncio.sleep(0.1)
        if _refresh_task and not _refresh_task.done():
            try:
                await asyncio.shield(_refresh_task)
            except Exception:
                pass  # a failed download must not suppress cached rematching
        while _cache_pending:
            _cache_pending = False
            await epg_svc.reingest_cached_sources()

    from ..database import spawn

    _cache_task = spawn(run(), name="epg-cache-rematch")


def start_refresh(job):
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        job.close()
        return {"ok": False, "error": "refresh already running"}
    from ..database import spawn

    _refresh_task = spawn(job, name="epg-refresh")
    return {"ok": True, "started": True}


class SourceIn(BaseModel):
    url: str


class ToggleIn(BaseModel):
    enabled: StrictBool | None = None
    refresh_hours: StrictInt | None = Field(default=None, ge=0, le=168)
    stale_hours: StrictInt | None = Field(default=None, ge=1, le=720)
    timezone_mode: TimezoneMode | None = None
    timezone_name: str | None = Field(default=None, max_length=64)
    offset_minutes: StrictInt | None = Field(default=None, ge=-1440, le=1440)


class AssignIn(BaseModel):
    epg_id: str | None


def _src_json(r: EpgSource, portals=None, global_hours=24, eligible=True) -> dict:
    from ..services.epg_policy import effective_hours, aware, stale_limit

    hours = effective_hours(r, global_hours)
    base = max((aware(d) for d in (r.last_fetch, r.last_attempt) if d), default=None)
    due = (base + timedelta(hours=hours)) if base else datetime.now(timezone.utc)
    if not r.enabled or not hours or not eligible:
        due = None
    return {
        "id": r.id,
        "url": r.url,
        "enabled": r.enabled,
        "portal_id": r.portal_id,
        "name": (
            (portals or {}).get(r.portal_id, "Missing portal")
            if r.url.startswith("portal://")
            else r.url
        ),
        "last_fetch": aware(r.last_fetch).isoformat() if r.last_fetch else None,
        "status": r.status,
        "channel_count": r.channel_count,
        "refresh_hours": r.refresh_hours,
        "effective_refresh_hours": hours,
        "stale_hours": r.stale_hours,
        "effective_stale_hours": stale_limit(r, global_hours),
        "last_attempt": aware(r.last_attempt).isoformat() if r.last_attempt else None,
        "last_error": r.last_error,
        "next_refresh": due.isoformat() if due else None,
        "timezone_mode": r.timezone_mode,
        "timezone_name": r.timezone_name,
        "offset_minutes": r.offset_minutes,
        "timing_pending": timing_pending(r),
    }


@router.get("")
async def overview(db=Depends(get_db)):
    sources = (
        (await db.execute(select(EpgSource).order_by(EpgSource.id))).scalars().all()
    )
    n_channels = await db.scalar(select(func.count()).select_from(EpgChannel)) or 0
    n_prog = await db.scalar(select(func.count()).select_from(EpgProgramme)) or 0
    n_live = await db.scalar(select(func.count()).select_from(LivePlaylist)) or 0
    n_matched = (
        await db.scalar(
            select(func.count())
            .select_from(LivePlaylist)
            .where(LivePlaylist.epg_id.isnot(None), LivePlaylist.epg_id != "")
        )
        or 0
    )
    portal_rows = (await db.scalars(select(Portal))).all()
    portals = {p.id: p.name for p in portal_rows}
    active_portals = {p.id for p in portal_rows if p.enabled}
    schedule = await epg_svc.schedule_info()
    return {
        "sources": [
            _src_json(
                r,
                portals,
                schedule["hours"],
                not r.url.startswith("portal://")
                or (schedule["portal_enabled"] and r.portal_id in active_portals),
            )
            for r in sources
        ],
        "schedule": schedule,
        "epg_channels": n_channels,
        "programmes": n_prog,
        "playlist_channels": n_live,
        "matched": n_matched,
        "refresh_running": bool(
            epg_svc.INGEST_LOCK.locked()
            or (_refresh_task and not _refresh_task.done())
            or (_cache_task and not _cache_task.done())
        ),
    }


@router.post("/sources")
async def add_source(body: SourceIn, db=Depends(get_db)):
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")
    exists = await db.scalar(select(EpgSource).where(EpgSource.url == url))
    if exists:
        raise HTTPException(409, "source already exists")
    row = EpgSource(url=url, enabled=True)
    db.add(row)
    await db.commit()
    await db_log("INFO", "epg", f"EPG source added: {url}")
    return {"id": row.id}


@router.delete("/sources/{src_id}")
async def delete_source(src_id: int, db=Depends(get_db)):
    row = await db.get(EpgSource, src_id)
    if not row:
        raise HTTPException(404, "not found")
    await db.execute(
        EpgChannelSource.__table__.delete().where(
            EpgChannelSource.epg_source_id == src_id
        )
    )
    await db.execute(
        EpgProgramme.__table__.delete().where(EpgProgramme.epg_source_id == src_id)
    )
    (
        await db.execute(
            EpgChannel.__table__.delete().where(EpgChannel.epg_source_id == src_id)
        )
    )
    await db.delete(row)
    await db.commit()
    epg_svc._cache_path(src_id, row.url).unlink(missing_ok=True)
    queue_cached_refresh()
    return {"ok": True}


@router.patch("/sources/{src_id}")
async def toggle_source(src_id: int, body: ToggleIn, db=Depends(get_db)):
    row = await db.get(EpgSource, src_id)
    if not row:
        raise HTTPException(404, "not found")
    for key in ("enabled", "timezone_mode", "offset_minutes"):
        if key in body.model_fields_set and getattr(body, key) is None:
            raise HTTPException(422, f"{key} cannot be null")
    old_key, old_offset = timing_key(row), row.offset_minutes
    timing_fields = {"timezone_mode", "timezone_name", "offset_minutes"}
    merged = {key: getattr(row, key) for key in timing_fields}
    merged.update(
        {key: getattr(body, key) for key in body.model_fields_set & timing_fields}
    )
    try:
        timing = SourceTiming.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    for key in body.model_fields_set - timing_fields:
        setattr(row, key, getattr(body, key))
    for key, value in timing.model_dump().items():
        setattr(row, key, value)
    await db.commit()
    changed = old_key != timing_key(row) or old_offset != row.offset_minutes
    if "enabled" in body.model_fields_set or changed:
        queue_cached_refresh()
    message = None
    if changed:
        if not row.enabled:
            message = "Timing saved. The source is disabled; cached timezone reprocessing runs when it is enabled."
        elif epg_svc._cache_path(row.id, row.url).exists():
            message = "Timing saved. Cached guide reprocessing queued; source correction applies immediately."
        else:
            message = "Timing saved. No cached guide: refresh this source to apply timezone interpretation. Source correction applies immediately."
    return {
        "ok": True,
        "enabled": row.enabled,
        "timing_pending": timing_pending(row),
        "message": message,
    }


@router.post("/sources/{src_id}/refresh")
async def refresh_source_endpoint(src_id: int, db=Depends(get_db)):
    row = await db.get(EpgSource, src_id)
    if not row:
        raise HTTPException(404, "not found")

    if not row.enabled:
        raise HTTPException(409, "Enable the source before refreshing")
    return start_refresh(epg_svc.refresh_source(src_id))


@router.post("/refresh")
async def refresh_all_endpoint():
    return start_refresh(epg_svc.refresh_all())


@router.post("/portals/check")
async def check_portals():
    if not (await epg_svc.schedule_info())["portal_enabled"]:
        raise HTTPException(409, "Enable portal EPG in Settings first")

    async def run():
        await epg_svc.ensure_portal_sources()
        from ..database import SessionLocal

        async with SessionLocal() as db:
            ids = (
                await db.scalars(
                    select(EpgSource.id).where(
                        EpgSource.portal_id.isnot(None), EpgSource.enabled.is_(True)
                    )
                )
            ).all()
        for source_id in ids:
            await epg_svc.refresh_source(source_id)

    return start_refresh(run())


@router.post("/match")
async def match_endpoint(review_all: bool = False):
    result = await epg_svc.match_report(review_all=review_all)
    if result["matched"]:
        queue_cached_refresh()
    return {"ok": True, **result}


@router.get("/suggest")
async def suggest(name: str = Query(min_length=1, max_length=300), db=Depends(get_db)):
    return {"items": epg_svc.rank_candidates(name, await epg_svc.epg_candidates(db))}


class MatchChoice(BaseModel):
    id: int
    epg_id: str = Field(min_length=1, max_length=200)
    previous: str | None = None


class MatchChoices(BaseModel):
    items: list[MatchChoice] = Field(max_length=5000)


@router.post("/match/assign")
async def apply_matches(body: MatchChoices, db=Depends(get_db)):
    known = {row["tvg_id"] for row in await epg_svc.epg_candidates(db)}
    if len({row.id for row in body.items}) != len(body.items):
        raise HTTPException(400, "Duplicate channel selections")
    for choice in body.items:
        row = await db.get(LivePlaylist, choice.id)
        if not row or not row.enabled:
            raise HTTPException(
                409, "A selected channel was removed or disabled; run matching again"
            )
        if (row.epg_id or "") != (choice.previous or ""):
            raise HTTPException(409, "A channel assignment changed; run matching again")
        if choice.epg_id not in known:
            raise HTTPException(400, "Selected EPG ID is not in an enabled guide")
        row.epg_id = choice.epg_id
    await db.commit()
    from ..services.playlist_gen import clear_m3u_cache

    clear_m3u_cache()
    queue_cached_refresh()
    return {"ok": True, "matched": len(body.items)}


@router.get("/channels")
async def list_epg_channels(
    q: str = "",
    source_id: int | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db=Depends(get_db),
):
    stmt = (
        select(EpgChannel)
        .join(EpgSource)
        .where(EpgSource.enabled.is_(True))
        .order_by(EpgChannel.name)
    )
    if source_id is not None:
        stmt = stmt.where(EpgChannel.epg_source_id == source_id)
    if q:
        stmt = stmt.where(
            EpgChannel.name.ilike(f"%{q}%") | EpgChannel.tvg_id.ilike(f"%{q}%")
        )
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        (await db.execute(stmt.offset((page - 1) * per_page).limit(per_page)))
        .scalars()
        .all()
    )
    return {
        "total": total or 0,
        "rows": [
            {
                "tvg_id": r.tvg_id,
                "name": r.name,
                "icon": r.icon,
                "source_id": r.epg_source_id,
            }
            for r in rows
        ],
    }


@router.patch("/channels/assign/{live_id}")
async def manual_assign(live_id: int, body: AssignIn, db=Depends(get_db)):
    """Manual override: assign (or clear) an EPG tvg-id for a playlist channel."""
    row = await db.get(LivePlaylist, live_id)
    if not row:
        raise HTTPException(404, "not found")
    row.epg_id = (body.epg_id or "").strip() or None
    await db.commit()
    from ..services.playlist_gen import clear_m3u_cache

    clear_m3u_cache()
    queue_cached_refresh()
    return {"ok": True, "epg_id": row.epg_id}


@router.get("/health")
async def guide_health(db=Depends(get_db)):
    from ..services.epg_policy import coverage_report

    result = await coverage_report(db)
    portals = {p.id: p.name for p in (await db.scalars(select(Portal))).all()}
    labels = {
        s.id: portals.get(s.portal_id, s.url)
        for s in (await db.scalars(select(EpgSource))).all()
    }
    for row in result["sources"]:
        row["name"] = labels.get(row["id"], row["name"])
    return result


@router.get("/policy/{live_id}")
async def get_policy(live_id: int, db=Depends(get_db)):
    from ..services.epg_policy import read_policy

    row = await db.get(LivePlaylist, live_id)
    if not row:
        raise HTTPException(404, "Channel not found")
    return {
        "policy": await read_policy(db, row),
        "output_epg_id": epg_svc.channel_epg_id(row),
        "explicit_sources": row.epg_sources_explicit,
    }


@router.put("/policy/{live_id}")
async def set_policy(live_id: int, body: ChannelGuide, db=Depends(get_db)):
    from ..services.epg_policy import apply_policy

    row = await db.get(LivePlaylist, live_id)
    if not row:
        raise HTTPException(404, "Channel not found")
    await apply_policy(db, row, body.model_dump())
    await db.commit()
    queue_cached_refresh()
    return {"ok": True, "output_epg_id": epg_svc.channel_epg_id(row)}


@router.get("/timezones")
async def source_timezones():
    from zoneinfo import available_timezones

    return {"timezones": sorted(available_timezones() - {"localtime", "posixrules"})}


class TimingPreview(SourceTiming):
    sample_start: str | None = Field(default=None, max_length=100)
    sample_stop: str | None = Field(default=None, max_length=100)


def _cached_sample(source):
    import io
    import xml.etree.ElementTree as ET

    path = epg_svc._cache_path(source.id, source.url)
    if not path.exists():
        return None
    raw = epg_svc._decode(path.read_bytes())
    root = None
    for event, elem in ET.iterparse(io.BytesIO(raw), events=("start", "end")):
        if root is None:
            if elem.tag != "tv":
                raise ValueError("Cached guide is not XMLTV")
            root = dict(elem.attrib)
        if event == "end" and elem.tag == "programme":
            if elem.get("start") and elem.get("stop"):
                return dict(elem.attrib), root, (elem.findtext("title") or "")[:400]
            elem.clear()
        elif event == "end" and elem.tag == "channel":
            elem.clear()
    return None


@router.post("/sources/{src_id}/timing-preview")
async def timing_preview(src_id: int, body: TimingPreview, db=Depends(get_db)):
    """Read-only, offline preview of unsaved source settings. Never download a feed."""
    from ..services.epg_timing import programme_inputs, programme_times

    source = await db.get(EpgSource, src_id)
    if not source:
        raise HTTPException(404, "Source not found")
    portal = source.url.startswith("portal://")
    try:
        if body.sample_start:
            root = {}
            attrs = {
                "start": body.sample_start,
                "stop": body.sample_stop or body.sample_start,
            }
            title = "Entered example"
            if portal:
                from ..portal.identity import STB_TIMEZONE

                row = (
                    await db.get(Portal, source.portal_id) if source.portal_id else None
                )
                root = {
                    "spm-raw-times": "1",
                    "spm-timezone": (row.stb_timezone if row else None) or STB_TIMEZONE,
                }
                attrs.update({"spm-start": attrs["start"], "spm-stop": attrs["stop"]})
        else:
            sample = await asyncio.to_thread(_cached_sample, source)
            if not sample:
                return {
                    "available": False,
                    "message": "No cached example. Enter a timestamp or refresh this source first.",
                }
            attrs, root, title = sample
        start, stop = programme_times(attrs, root, body, portal=portal)
        try:
            automatic, _ = programme_times(attrs, root, SourceTiming(), portal=portal)
            automatic_utc = automatic.utc
        except (ValueError, OverflowError):
            automatic_utc = None
        original_start, original_stop, _ = programme_inputs(attrs, root, portal=portal)
        warnings = [t.warning for t in (start, stop) if t.warning]
        if stop.utc <= start.utc:
            warnings.append(
                "Example stop is not after start; such programmes are excluded from the guide"
            )
        shift = timedelta(minutes=body.offset_minutes)
        return {
            "available": True,
            "title": title,
            "channel": attrs.get("channel"),
            "original_start": original_start,
            "original_stop": original_stop,
            "automatic_utc": automatic_utc,
            "interpreted_utc": start.utc,
            "corrected_start": start.utc + shift,
            "corrected_stop": stop.utc + shift,
            "warnings": list(dict.fromkeys(warnings)),
        }
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        # Cached XML may have been deleted or damaged during a maintenance job.
        # Do not expose filesystem paths or make a preview modify source status.
        raise HTTPException(
            422,
            "Unable to read the cached example; enter a timestamp or refresh the source",
        ) from exc
