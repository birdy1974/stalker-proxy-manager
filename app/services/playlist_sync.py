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
from .titles import best_title

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
            row = VodPlaylist(vod_source_id=src.id, custom_name=best_title(src.original_name),
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
            row = SeriePlaylist(serie_source_id=src.id, custom_name=best_title(src.original_name),
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


def _name_key(s: str | None) -> str:
    return (s or "").strip().casefold()


async def live_playlist_links_for(db, source_ids: list[int]) -> dict[int, dict]:
    """Map live_source_id -> playlist placement used by Input Sources → Live.

    One query for the page of ids. Each value is
      {playlist_id, custom_name, priority, is_primary, chain_len}
    or missing when the source is not (yet) on any live playlist row.
    """
    ids = _clean_ids(source_ids)
    if not ids:
        return {}
    rows = (await db.execute(
        select(LivePlaylistSource.live_source_id, LivePlaylistSource.priority,
               LivePlaylist.id, LivePlaylist.custom_name)
        .join(LivePlaylist, LivePlaylist.id == LivePlaylistSource.live_playlist_id)
        .where(LivePlaylistSource.live_source_id.in_(ids))
        .order_by(LivePlaylistSource.priority))).all()
    # Prefer the lowest-priority (primary) link when a source sits on several chains.
    best: dict[int, tuple] = {}
    for sid, prio, pid, cname in rows:
        cur = best.get(sid)
        if cur is None or prio < cur[0]:
            best[sid] = (prio, pid, cname)
    if not best:
        return {}
    # chain lengths for the playlists we care about (badge: "fallback #2 of 3")
    pl_ids = {v[1] for v in best.values()}
    lengths: dict[int, int] = {}
    for pid, n in (await db.execute(
            select(LivePlaylistSource.live_playlist_id, func.count())
            .where(LivePlaylistSource.live_playlist_id.in_(pl_ids))
            .group_by(LivePlaylistSource.live_playlist_id))).all():
        lengths[pid] = n
    return {
        sid: {
            "playlist_id": pid,
            "custom_name": cname,
            "priority": prio,
            "is_primary": prio == 1,
            "chain_len": lengths.get(pid, 1),
        }
        for sid, (prio, pid, cname) in best.items()
    }


async def _detach_live_source(db, source_id: int, *, keep_playlist_id: int | None = None) -> list[int]:
    """Remove this source from every live-playlist chain (except `keep`).

    Empty custom channels left behind are deleted so a rename-to-fallback does
    not leave orphan playlist rows with no sources.
    """
    links = (await db.execute(select(LivePlaylistSource).where(
        LivePlaylistSource.live_source_id == source_id))).scalars().all()
    touched: set[int] = set()
    for link in links:
        if keep_playlist_id is not None and link.live_playlist_id == keep_playlist_id:
            continue
        touched.add(link.live_playlist_id)
        await db.delete(link)
    await db.flush()
    emptied: list[int] = []
    for pid in touched:
        left = await db.scalar(select(func.count()).select_from(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == pid))
        if not left:
            row = await db.get(LivePlaylist, pid)
            if row:
                await db.delete(row)
                emptied.append(pid)
    return emptied


async def assign_live_custom_name(db, source_id: int, custom_name: str) -> dict:
    """
    Input Sources → Live: set the Playlist custom name for an enabled source.

    * name unique among live playlist rows  → create a new custom channel
      (or rename the channel this source already owns as primary)
    * name already used                     → attach this source as a fallback
      on that existing custom channel (and detach it from any other chain)

    Matching is case-insensitive and whitespace-trimmed. Does not commit.
    """
    name = (custom_name or "").strip()
    if not name:
        raise ValueError("custom name required")
    src = await db.get(LiveSource, source_id)
    if not src:
        raise ValueError("source not found")
    if not src.enabled:
        raise ValueError("enable the channel first")

    # Existing playlist row with this custom name (case-insensitive).
    match: LivePlaylist | None = None
    for row in (await db.execute(select(LivePlaylist))).scalars().all():
        if _name_key(row.custom_name) == _name_key(name):
            match = row
            break

    if match is not None:
        # Already on this chain? Keep position; just report.
        on = (await db.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == match.id,
            LivePlaylistSource.live_source_id == source_id))).scalar_one_or_none()
        if on is not None:
            return {"action": "unchanged", "playlist_id": match.id,
                    "custom_name": match.custom_name, "priority": on.priority,
                    "is_primary": on.priority == 1}
        await _detach_live_source(db, source_id, keep_playlist_id=match.id)
        max_p = await db.scalar(select(func.max(LivePlaylistSource.priority)).where(
            LivePlaylistSource.live_playlist_id == match.id)) or 0
        prio = int(max_p) + 1
        db.add(LivePlaylistSource(live_playlist_id=match.id, live_source_id=source_id,
                                  priority=prio))
        await db.flush()
        return {"action": "fallback", "playlist_id": match.id,
                "custom_name": match.custom_name, "priority": prio, "is_primary": False}

    # Unique name: rename the primary channel this source already owns, else create.
    primary = (await db.execute(select(LivePlaylistSource).where(
        LivePlaylistSource.live_source_id == source_id,
        LivePlaylistSource.priority == 1))).scalar_one_or_none()
    if primary is not None:
        pl = await db.get(LivePlaylist, primary.live_playlist_id)
        if pl:
            pl.custom_name = name
            await db.flush()
            return {"action": "renamed", "playlist_id": pl.id, "custom_name": pl.custom_name,
                    "priority": 1, "is_primary": True}

    await _detach_live_source(db, source_id)
    genre = await db.get(LiveGenre, src.live_genre_id) if src.live_genre_id else None
    nxt = await _next_order(db, LivePlaylist, 1)
    pl = LivePlaylist(
        custom_name=name,
        group_name=(genre.name if genre else None) or "Live",
        number=int(src.number) if str(src.number or "").isdigit() else None,
        epg_id=src.epg_original, logo=src.logo_original,
        enabled=True, order=nxt,
    )
    db.add(pl)
    await db.flush()
    db.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=source_id, priority=1))
    await db.flush()
    return {"action": "created", "playlist_id": pl.id, "custom_name": pl.custom_name,
            "priority": 1, "is_primary": True}


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


async def sync_season_links(db, playlist_ids: list[int] | None = None) -> int:
    """
    Create the `serie_playlist_seasons` rows that are missing, and report how
    many were added.

    A playlist-series only contributes episodes to the output through its
    season link rows. Those rows used to be created in exactly two places:
    when the series was added to the playlist, and by a read-repair inside
    `GET /api/playlist/series` - i.e. only when someone opened the Series tab
    in the Playlist Builder. So the normal order of operations (add series to
    the playlist, *then* fetch its seasons) left the table empty and the series
    silently contributed nothing to any user's playlist, with no error anywhere.
    "There is no possibility to fetch and select seasons for series" is exactly
    this.

    Batched: three queries regardless of how many series there are.
    """
    from ..models import SerieEpisode, SeriePlaylist, SeriePlaylistSeason, SerieSeason

    q = select(SeriePlaylist.id, SeriePlaylist.serie_source_id)
    if playlist_ids is not None:
        if not playlist_ids:
            return 0
        q = q.where(SeriePlaylist.id.in_(playlist_ids))
    items = (await db.execute(q)).all()
    if not items:
        return 0

    seasons_by_src: dict[int, list] = {}
    for src_id, season_id, enabled in (await db.execute(
            select(SerieSeason.serie_source_id, SerieSeason.id, SerieSeason.enabled)
            .where(SerieSeason.serie_source_id.in_({src for _, src in items})))).all():
        seasons_by_src.setdefault(src_id, []).append((season_id, enabled))

    have = set()
    for pid, season_id in (await db.execute(
            select(SeriePlaylistSeason.serie_playlist_id, SeriePlaylistSeason.serie_season_id)
            .where(SeriePlaylistSeason.serie_playlist_id.in_([p for p, _ in items])))).all():
        have.add((pid, season_id))

    added = 0
    for pid, src_id in items:
        for season_id, enabled in seasons_by_src.get(src_id, []):
            if (pid, season_id) in have:
                continue
            # follow the source season's own enabled flag, so a season switched
            # off in Input Sources does not sneak into the output
            db.add(SeriePlaylistSeason(serie_playlist_id=pid, serie_season_id=season_id,
                                       enabled=bool(enabled)))
            added += 1
    if added:
        await db.commit()
    return added
