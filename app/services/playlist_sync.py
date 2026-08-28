"""
Input Sources -> Playlist Builder sync (bulk, one transaction).

The Playlist Builder has always promised this ("Enable VOD items in Input
Sources -> VOD, they'll appear here ... (auto on enable)"), but nothing did it:
`POST /api/sources/toggle` only flipped the flag on the source row and the
`add-from-source` endpoint that creates the playlist rows was never called by
the GUI. Result: an enabled VOD/series/local file never showed up in the
Playlist Builder - exactly the "enabled vod does not appear" report.

Rules
  * source enabled  -> create the playlist row (idempotent) or switch it on
  * source disabled -> keep the row and its edits, but switch it off, so a
    later re-enable restores it without losing the group/template the user set
  * live channels are NOT auto-created: they are curated custom channels with
    an ordered fallback chain, so they are added explicitly (in bulk, see
    `add_sources`) instead of implicitly

Everything runs in ONE transaction and with a handful of queries per call - the
old per-item path (one session + commit per row) is what made bulk operations
on the persistent database so slow.
"""

from __future__ import annotations

from sqlalchemy import func, select

from ..models import (LiveGenre, LivePlaylist, LivePlaylistSource, LiveSource,
                      LocalFile, LocalPlaylist, SerieGenre, SeriePlaylist,
                      SeriePlaylistSeason, SeriePlaylistSource, SerieSeason,
                      SerieSource, VodGenre, VodPlaylist, VodPlaylistSource,
                      VodSource)

# kinds whose "enabled" switch mirrors straight into the output playlist
SYNC_KINDS = ("vod", "series", "local")
# kinds that can be pushed into the playlist from an explicit (bulk) action
ADD_KINDS = ("live", "vod", "series", "local")


def _clean_ids(ids) -> list[int]:
    out: list[int] = []
    for i in ids or []:
        try:
            out.append(int(i))
        except (TypeError, ValueError):
            continue
    return out


async def _next_order(db, model, start: int) -> int:
    cur = await db.scalar(select(func.max(model.order)))
    return (cur or 0) + start


# --------------------------------------------------------------------- vod
async def _sync_vod(db, ids: list[int], enabled: bool) -> dict:
    srcs = (await db.execute(select(VodSource).where(VodSource.id.in_(ids)))).scalars().all()
    existing = {r.vod_source_id: r for r in (await db.execute(
        select(VodPlaylist).where(VodPlaylist.vod_source_id.in_(ids)))).scalars().all()}
    nxt = await _next_order(db, VodPlaylist, 1)
    created = enabled_n = disabled_n = 0
    for src in srcs:
        row = existing.get(src.id)
        if row is None:
            if not enabled:
                continue                      # nothing to switch off
            genre = await db.get(VodGenre, src.vod_genre_id) if src.vod_genre_id else None
            row = VodPlaylist(vod_source_id=src.id, custom_name=src.original_name,
                              group_name=(genre.name if genre else None) or src.genre or "VOD",
                              poster=src.poster, year=src.year, rating=src.rating,
                              overview=src.description, enabled=True, order=nxt)
            db.add(row)
            await db.flush()                  # need the id for the link row
            db.add(VodPlaylistSource(vod_playlist_id=row.id, vod_source_id=src.id, priority=1))
            created += 1
            nxt += 1
            continue
        if row.enabled != enabled:
            row.enabled = enabled
            enabled_n += 1 if enabled else 0
            disabled_n += 0 if enabled else 1
    return {"created": created, "enabled": enabled_n, "disabled": disabled_n}


# ------------------------------------------------------------------ series
async def _sync_series(db, ids: list[int], enabled: bool) -> dict:
    srcs = (await db.execute(select(SerieSource).where(SerieSource.id.in_(ids)))).scalars().all()
    existing = {r.serie_source_id: r for r in (await db.execute(
        select(SeriePlaylist).where(SeriePlaylist.serie_source_id.in_(ids)))).scalars().all()}
    nxt = await _next_order(db, SeriePlaylist, 1)
    created = enabled_n = disabled_n = 0
    for src in srcs:
        row = existing.get(src.id)
        if row is None:
            if not enabled:
                continue
            genre = await db.get(SerieGenre, src.serie_genre_id) if src.serie_genre_id else None
            row = SeriePlaylist(serie_source_id=src.id, custom_name=src.original_name,
                                group_name=(genre.name if genre else None)
                                or src.category_name or "Series",
                                poster=src.poster, year=src.year, rating=src.rating,
                                overview=src.description, enabled=True, order=nxt)
            db.add(row)
            await db.flush()
            db.add(SeriePlaylistSource(serie_playlist_id=row.id, serie_source_id=src.id, priority=1))
            seasons = (await db.execute(select(SerieSeason).where(
                SerieSeason.serie_source_id == src.id))).scalars().all()
            for sn in seasons:
                db.add(SeriePlaylistSeason(serie_playlist_id=row.id,
                                           serie_season_id=sn.id, enabled=sn.enabled))
            created += 1
            nxt += 1
            continue
        if row.enabled != enabled:
            row.enabled = enabled
            enabled_n += 1 if enabled else 0
            disabled_n += 0 if enabled else 1
    return {"created": created, "enabled": enabled_n, "disabled": disabled_n}


# ------------------------------------------------------------------- local
async def _sync_local(db, ids: list[int], enabled: bool) -> dict:
    """`ids` are local_files ids (the Local playlist references files)."""
    files = (await db.execute(select(LocalFile).where(LocalFile.id.in_(ids)))).scalars().all()
    existing = {r.local_file_id: r for r in (await db.execute(
        select(LocalPlaylist).where(LocalPlaylist.local_file_id.in_(ids)))).scalars().all()}
    nxt = await _next_order(db, LocalPlaylist, 1)
    created = enabled_n = disabled_n = 0
    for f in files:
        row = existing.get(f.id)
        if row is None:
            if not enabled:
                continue
            db.add(LocalPlaylist(local_file_id=f.id, custom_name=f.filename,
                                 group_name="vod-local", enabled=True, order=nxt))
            created += 1
            nxt += 1
            continue
        if row.enabled != enabled:
            row.enabled = enabled
            enabled_n += 1 if enabled else 0
            disabled_n += 0 if enabled else 1
    return {"created": created, "enabled": enabled_n, "disabled": disabled_n}


# ------------------------------------------------------------------- public
async def sync_sources(db, kind: str, ids, enabled: bool) -> dict:
    """Mirror an Input-Sources switch into the output playlist (no commit)."""
    ids = _clean_ids(ids)
    if kind not in SYNC_KINDS or not ids:
        return {"kind": kind, "created": 0, "enabled": 0, "disabled": 0}
    if kind == "vod":
        out = await _sync_vod(db, ids, enabled)
    elif kind == "series":
        out = await _sync_series(db, ids, enabled)
    else:
        out = await _sync_local(db, ids, enabled)
    return {"kind": kind, **out}


async def add_sources(db, kind: str, ids, group: str | None = None) -> dict:
    """
    Push sources into the playlist explicitly, in bulk (one transaction).

    Used by the Playlist Builder's "Add from sources" dialog - picking a whole
    genre/group in one go beats adding items one by one. Existing rows are left
    untouched and reported as `existed`.
    """
    ids = _clean_ids(ids)
    if kind not in ADD_KINDS or not ids:
        return {"kind": kind, "added": 0, "existed": 0, "missing": 0}
    if kind == "live":
        return {"kind": kind, **await _add_live(db, ids, group)}
    before = await _existing_ids(db, kind, ids)
    if kind == "vod":
        out = await _sync_vod(db, [i for i in ids if i not in before], True)
    elif kind == "series":
        out = await _sync_series(db, [i for i in ids if i not in before], True)
    else:
        out = await _sync_local(db, [i for i in ids if i not in before], True)
    known = await _known_source_count(db, kind, ids)
    return {"kind": kind, "added": out["created"], "existed": len(before),
            "missing": max(0, len(ids) - known)}


async def _add_live(db, ids: list[int], group: str | None) -> dict:
    """One custom channel per selected live source (chain = that source)."""
    existing = {r.live_source_id for r in (await db.execute(
        select(LivePlaylistSource).where(LivePlaylistSource.live_source_id.in_(ids),
                                         LivePlaylistSource.priority == 1))).scalars().all()}
    srcs = (await db.execute(select(LiveSource).where(LiveSource.id.in_(ids)))).scalars().all()
    nxt = await _next_order(db, LivePlaylist, 1)
    added = 0
    for src in srcs:
        if src.id in existing:
            continue
        genre = await db.get(LiveGenre, src.live_genre_id) if src.live_genre_id else None
        row = LivePlaylist(custom_name=src.original_name,
                           group_name=group or (genre.name if genre else None) or "Live",
                           number=int(src.number) if str(src.number or "").isdigit() else None,
                           epg_id=src.epg_original, logo=src.logo_original,
                           enabled=True, order=nxt)
        db.add(row)
        await db.flush()
        db.add(LivePlaylistSource(live_playlist_id=row.id, live_source_id=src.id, priority=1))
        added += 1
        nxt += 1
    return {"added": added, "existed": len(existing), "missing": max(0, len(ids) - len(srcs))}


async def _existing_ids(db, kind: str, ids: list[int]) -> set[int]:
    model, col = {
        "vod": (VodPlaylist, VodPlaylist.vod_source_id),
        "series": (SeriePlaylist, SeriePlaylist.serie_source_id),
        "local": (LocalPlaylist, LocalPlaylist.local_file_id),
    }[kind]
    rows = (await db.execute(select(col).where(col.in_(ids)))).all()
    return {r[0] for r in rows}


async def _known_source_count(db, kind: str, ids: list[int]) -> int:
    model = {"vod": VodSource, "series": SerieSource, "local": LocalFile,
             "live": LiveSource}[kind]
    return (await db.scalar(select(func.count()).select_from(model).where(model.id.in_(ids)))) or 0
