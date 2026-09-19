"""Per-channel guide selection, gap filling and coverage diagnostics.

All stored timestamps remain UTC and unshifted. Corrections are applied at read
 time, identically for XMLTV and alerts; changing an offset never compounds it.
"""

from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictInt, ValidationError
from sqlalchemy import delete, select

from ..models import (
    EpgChannel,
    EpgChannelSource,
    EpgProgramme,
    EpgSource,
    LivePlaylist,
    Portal,
)


class GuideMapping(BaseModel):
    source_id: StrictInt = Field(gt=0)
    tvg_id: str = Field(min_length=1, max_length=200)
    offset_minutes: StrictInt = Field(default=0, ge=-1440, le=1440)


class ChannelGuide(BaseModel):
    gap_fill: StrictBool = True
    offset_minutes: StrictInt = Field(default=0, ge=-1440, le=1440)
    mappings: list[GuideMapping] = Field(default_factory=list, max_length=32)


async def apply_policy(db, item, payload):
    try:
        policy = ChannelGuide.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    keys = [(r.source_id, r.tvg_id.strip()) for r in policy.mappings]
    if any(not cid for _, cid in keys) or len(set(keys)) != len(keys):
        raise HTTPException(
            400, "Guide IDs must be nonempty and mappings must be unique"
        )
    source_ids = {sid for sid, _ in keys}
    known = set(
        (
            await db.scalars(select(EpgSource.id).where(EpgSource.id.in_(source_ids)))
        ).all()
    )
    if known != source_ids:
        raise HTTPException(400, "An EPG source was removed; reload the guide editor")
    await db.execute(
        delete(EpgChannelSource).where(EpgChannelSource.live_playlist_id == item.id)
    )
    for priority, row in enumerate(policy.mappings):
        db.add(
            EpgChannelSource(
                live_playlist_id=item.id,
                epg_source_id=row.source_id,
                tvg_id=row.tvg_id.strip(),
                priority=priority,
                offset_minutes=row.offset_minutes,
            )
        )
    item.epg_gap_fill = policy.gap_fill
    item.epg_offset_minutes = policy.offset_minutes
    item.epg_sources_explicit = bool(policy.mappings)
    item.epg_custom = bool(
        policy.mappings or policy.offset_minutes or not policy.gap_fill
    )
    item.epg_has_mappings = bool(policy.mappings)
    return policy


async def read_policy(db, item):
    rows = (
        await db.scalars(
            select(EpgChannelSource)
            .where(EpgChannelSource.live_playlist_id == item.id)
            .order_by(EpgChannelSource.priority, EpgChannelSource.id)
        )
    ).all()
    return {
        "gap_fill": bool(item.epg_gap_fill),
        "offset_minutes": item.epg_offset_minutes or 0,
        "mappings": [
            {
                "source_id": r.epg_source_id,
                "tvg_id": r.tvg_id,
                "offset_minutes": r.offset_minutes,
            }
            for r in rows
        ],
    }


def aware(dt):
    return (
        dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt
    )


def effective_hours(source, global_hours):
    # Global pause is a master switch; a per-source zero means manual only.
    if not global_hours:
        return 0
    return global_hours if source.refresh_hours is None else source.refresh_hours


def stale_limit(source, global_hours):
    return source.stale_hours or max(
        48, 2 * (effective_hours(source, global_hours) or 24)
    )


def source_health(source, global_hours, now):
    from .epg_timing import timing_pending

    flags = []
    if timing_pending(source):
        flags.append("timing_pending")
    if not source.last_fetch:
        flags.append("never_fetched")
    elif now - aware(source.last_fetch) >= timedelta(
        hours=stale_limit(source, global_hours)
    ):
        flags.append("stale")
    if source.last_error or (source.status or "").startswith("failed:"):
        flags.append("refresh_failed")
    return flags


@dataclass(frozen=True)
class GuideEvent:
    programme: EpgProgramme
    start: datetime
    stop: datetime
    source_id: int | None


def fill_gaps(layers, *, fill=True):
    """Priority order wins. Lower-priority entries are clipped to uncovered spans.

    This includes overlaps inside a feed (newest record wins at equal starts).
    Returned intervals are non-overlapping and keep the original metadata.
    """
    events, starts = [], []
    for layer in (layers if fill else layers[:1]):
        for event in sorted(layer, key=lambda p: (p.start, -(p.programme.id or 0))):
            if event.stop <= event.start:
                continue
            cursor = event.start
            i = max(0, bisect_left(starts, cursor) - 1)
            fragments = []
            while i < len(events) and events[i].start < event.stop:
                covered = events[i]
                if covered.stop > cursor:
                    if covered.start > cursor:
                        fragments.append((cursor, min(covered.start, event.stop)))
                    cursor = max(cursor, covered.stop)
                i += 1
            if cursor < event.stop:
                fragments.append((cursor, event.stop))
            for start, stop in fragments:
                position = bisect_left(starts, start)
                starts.insert(position, start)
                events.insert(position, replace(event, start=start, stop=stop))
    return events


async def load_schedules(db, items, now=None, horizon_hours=48):
    """One shared resolver for XMLTV and coverage checks; no network calls."""
    from .runtime_settings import get_setting

    now = now or datetime.now(timezone.utc)
    hi = now + timedelta(hours=horizon_hours)
    sources = {
        s.id: s
        for s in (await db.scalars(select(EpgSource).order_by(EpgSource.id))).all()
    }
    portals = set(
        (await db.scalars(select(Portal.id).where(Portal.enabled.is_(True)))).all()
    )
    portal_on = await get_setting("epg_portal_enabled", True)
    active = {
        sid
        for sid, s in sources.items()
        if s.enabled
        and (
            not s.url.startswith("portal://") or (portal_on and s.portal_id in portals)
        )
    }
    mappings = {}
    item_ids = [it.id for it in items]
    for i in range(0, len(item_ids), 500):
        rows = (
            await db.scalars(
                select(EpgChannelSource)
                .where(EpgChannelSource.live_playlist_id.in_(item_ids[i : i + 500]))
                .order_by(EpgChannelSource.priority, EpgChannelSource.id)
            )
        ).all()
        for row in rows:
            mappings.setdefault(row.live_playlist_id, []).append(row)
    ids = {it.epg_id for it in items if it.epg_id}
    ids.update(r.tvg_id for rows in mappings.values() for r in rows)
    by_key = {}
    available = set()
    ids = list(ids)
    for i in range(0, len(ids), 500):
        available.update(
            (
                await db.execute(
                    select(EpgChannel.epg_source_id, EpgChannel.tvg_id).where(
                        EpgChannel.tvg_id.in_(ids[i : i + 500])
                    )
                )
            ).all()
        )
        rows = (
            await db.scalars(
                select(EpgProgramme).where(
                    EpgProgramme.tvg_id.in_(ids[i : i + 500]),
                    EpgProgramme.stop_ts > now - timedelta(hours=72),
                    EpgProgramme.start_ts < hi + timedelta(hours=72),
                    EpgProgramme.epg_source_id.is_(None)
                    | EpgProgramme.epg_source_id.in_(active),
                )
            )
        ).all()
        for row in rows:
            by_key.setdefault((row.epg_source_id, row.tvg_id), []).append(row)
    schedules, used = {}, {}
    for item in items:
        rules = mappings.get(item.id, [])
        if rules or getattr(item, "epg_sources_explicit", False):
            selected = [
                (r.epg_source_id, r.tvg_id, r.offset_minutes)
                for r in rules
                if r.epg_source_id in active
            ]
        else:
            selected = [
                (sid, item.epg_id, 0)
                for sid in sorted(active)
                if (sid, item.epg_id) in available or (sid, item.epg_id) in by_key
            ]
            # Legacy unattributed events are a last-resort fallback, never used
            # for an explicitly configured list of guide sources.
            selected.append((None, item.epg_id, 0))
        if not item.epg_gap_fill:
            selected = selected[:1]
        layers = []
        for sid, tvg, correction in selected:
            shift = timedelta(
                minutes=(item.epg_offset_minutes or 0)
                + (correction or 0)
                + ((sources[sid].offset_minutes or 0) if sid in sources else 0)
            )
            layer = [
                GuideEvent(p, aware(p.start_ts) + shift, aware(p.stop_ts) + shift, sid)
                for p in by_key.get((sid, tvg), [])
            ]
            layers.append([e for e in layer if e.stop > now and e.start < hi])
        result = fill_gaps(layers)
        schedules[item.id] = result
        used[item.id] = {e.source_id for e in result if e.source_id is not None}
    return schedules, used, sources, active


async def coverage_report(db, now=None):
    from .epg import schedule_info, channel_epg_id

    now = now or datetime.now(timezone.utc)
    items = (
        await db.scalars(
            select(LivePlaylist)
            .where(LivePlaylist.enabled.is_(True))
            .order_by(LivePlaylist.order, LivePlaylist.id)
        )
    ).all()
    schedules, used, sources, active = await load_schedules(db, items, now)
    global_hours = (await schedule_info())["hours"]
    source_flags = {
        sid: source_health(sources[sid], global_hours, now) for sid in active
    }
    alerts, counts = [], {"missing": 0, "gap_now": 0, "stale": 0}
    for item in items:
        programmes = schedules[item.id]
        flags = []
        if not programmes:
            flags.append("missing")
        elif not any(p.start <= now < p.stop for p in programmes):
            flags.append("gap_now")
        if used[item.id] and all(
            any(f in source_flags[sid] for f in ("stale", "never_fetched"))
            for sid in used[item.id]
        ):
            flags.append("stale")
        for flag in flags:
            counts[flag] += 1
        if flags:
            alerts.append(
                {
                    "id": item.id,
                    "name": item.custom_name,
                    "epg_id": channel_epg_id(item),
                    "flags": flags,
                    "last_programme_end": max(
                        (p.stop for p in programmes), default=None
                    ),
                }
            )
    return {
        "checked_at": now,
        "channels_checked": len(items),
        "counts": counts,
        "channels": alerts,
        "sources": [
            {
                "id": sid,
                "name": sources[sid].url,
                "flags": flags,
                "last_fetch": sources[sid].last_fetch,
                "error": sources[sid].last_error or sources[sid].status,
            }
            for sid, flags in sorted(source_flags.items())
            if flags
        ],
    }
