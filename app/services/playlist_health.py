"""Read-only playlist diagnostics; no automatic portal requests or media opens.

Checks mirror the playback chains, including portal_first and episode coordinates.
Input-selection flags are warnings, not playback gates for explicit playlist links.
"""

import asyncio
from collections import Counter, defaultdict, OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import os
import stat
import time

from sqlalchemy import select
from sqlalchemy.orm import load_only

from ..models import (
    Portal,
    MacAddress,
    LivePlaylist,
    LivePlaylistSource,
    LiveSource,
    VodPlaylist,
    VodPlaylistSource,
    VodSource,
    SeriePlaylist,
    SeriePlaylistSource,
    SeriePlaylistSeason,
    SerieSource,
    SerieSeason,
    SerieEpisode,
    LocalPlaylist,
    LocalFile,
    LocalSource,
)
from ..portal.account import mac_is_usable, parse_expiry
from .item_info import local_file_path

EVIDENCE_TTL = 900
_OBSERVATIONS = OrderedDict()
_files_task = None
_files_paths = None
_files_result = {}
_files_at = 0.0
KINDS = ("live", "vod", "series", "local")


def signature(source, path=None):
    source = getattr(source, "_src", source)
    fields = [
        getattr(source, k, None)
        for k in (
            "portal_id",
            "cmd",
            "media_cmd",
            "xtream_url",
            "link_flags",
            "series_param",
            "serie_season_id",
            "episode_number",
            "relative_path",
            "mtime",
            "size_bytes",
        )
    ]
    digest = hashlib.sha256(repr([fields, path]).encode()).hexdigest()[:24]
    return type(source).__name__, getattr(source, "id", 0), digest


def record_probe(key, result):
    if not key:
        return
    # A missing local tool isn't evidence that a provider's stream is broken.
    error = str(result.get("error") or "")
    if (
        "start ffprobe" in error
        or "binary" in error
        or "not found" in error.lower()
        and "ffmpeg" in error.lower()
    ):
        return
    media = bool(result.get("video") or result.get("audio"))
    verdict = "healthy" if media and not error else "failed"
    _remember(key, verdict, EVIDENCE_TTL, "media probe")


def record_playback(source, *, failed=False, ttl=EVIDENCE_TTL):
    _remember(signature(source), "warning" if failed else "healthy", ttl, "playback")


def _remember(key, verdict, ttl, method):
    now = time.monotonic()
    for old, entry in list(_OBSERVATIONS.items()):
        if now - entry[0] >= entry[2]:
            _OBSERVATIONS.pop(old, None)
    _OBSERVATIONS[key] = (now, verdict, ttl, method)
    _OBSERVATIONS.move_to_end(key)
    while len(_OBSERVATIONS) > 4096:
        _OBSERVATIONS.popitem(last=False)


def _inspect_files(paths):
    result = {}
    for path in paths:
        try:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode):
                reason = "Not a regular file"
            elif not os.access(path, os.R_OK):
                reason = "File is not readable"
            elif st.st_size == 0:
                reason = "File is empty"
            else:
                reason = None
        except FileNotFoundError:
            reason = "File or mounted directory is missing"
        except OSError:
            reason = "File is inaccessible"
        result[path] = reason
    return result


async def inspect_files(paths):
    """One shared worker at most, so a stalled mount cannot spawn endless scans."""
    global _files_task, _files_paths, _files_result, _files_at
    paths = tuple(sorted(set(paths)))
    if not paths:
        return {}
    if _files_paths == paths and time.monotonic() - _files_at < 15:
        return _files_result
    if _files_task is None or _files_task.done():
        _files_paths = paths
        _files_at = 0.0
        _files_result = {}

        async def scan():
            global _files_result, _files_at
            _files_result = await asyncio.to_thread(_inspect_files, paths)
            _files_at = time.monotonic()
            return _files_result

        _files_task = asyncio.create_task(scan())
    elif _files_paths != paths:
        return {}  # another scan is still stuck; report unchecked, not missing
    try:
        return await asyncio.wait_for(asyncio.shield(_files_task), 2)
    except (asyncio.TimeoutError, OSError):
        return {}


async def rows_for(db, model, field, ids):
    rows = []
    ids = list(set(ids))
    for i in range(0, len(ids), 500):
        stmt = select(model).where(field.in_(ids[i : i + 500]))
        # Series raw metadata/posters and account credentials aren't diagnostics.
        if model is SerieSource:
            stmt = stmt.options(
                load_only(
                    SerieSource.id,
                    SerieSource.portal_id,
                    SerieSource.original_name,
                    SerieSource.enabled,
                )
            )
        rows.extend((await db.scalars(stmt)).all())
    return rows


def aggregate(candidates):
    if not candidates:
        return "unavailable"
    states = {c["status"] for c in candidates if c["status"] != "skipped"}
    if not states:
        return "unavailable"
    if states <= {"unavailable", "failed"}:
        return "unavailable"
    if states & {"unavailable", "failed", "warning"}:
        return "warning"
    return "healthy" if "healthy" in states else "unverified"


async def report(db):
    from .stream_manager import MANAGER
    from .runtime_settings import fallback_strategy

    strategy = await fallback_strategy()
    now, mono = datetime.now(timezone.utc), time.monotonic()
    portals = {
        p.id: p
        for p in (
            await db.scalars(
                select(Portal).options(
                    load_only(
                        Portal.id, Portal.name, Portal.enabled, Portal.resolved_url
                    )
                )
            )
        ).all()
    }
    macs = defaultdict(list)
    for mac in (
        await db.scalars(
            select(MacAddress)
            .options(
                load_only(
                    MacAddress.id,
                    MacAddress.portal_id,
                    MacAddress.order,
                    MacAddress.status,
                    MacAddress.last_checked,
                    MacAddress.expire_date,
                )
            )
            .order_by(MacAddress.order, MacAddress.id)
        )
    ).all():
        macs[mac.portal_id].append(mac)

    def candidate(src, portal_id, kind, *, name=None, path=None, used=None):
        result = {
            "kind": kind,
            "id": src.id if src else None,
            "name": name
            or (
                getattr(src, "original_name", None)
                or getattr(src, "name", None)
                or "Missing source"
            ),
            "portal": portals[portal_id].name if portal_id in portals else None,
            "status": "unverified",
            "reasons": [],
            "checked_at": None,
        }
        reasons = result["reasons"]

        def blocked(reason):
            result["status"] = "unavailable"
            reasons.append(reason)
            return result

        if src is None:
            return blocked("Source record is missing")
        if not (getattr(src, "cmd", None) or "").strip():
            return blocked("Source has no stream command")
        portal = portals.get(portal_id)
        if not portal:
            return blocked("Portal is missing")
        if not portal.enabled:
            return blocked("Portal is disabled")
        eligible = [m for m in macs[portal.id] if mac_is_usable(m.status)]
        if not eligible:
            return blocked(
                "No MAC accounts configured"
                if not macs[portal.id]
                else "All MAC accounts are banned or expired"
            )
        if strategy == "portal_first":
            if portal.id in used:
                result["status"] = "skipped"
                reasons.append("Skipped by portal-first fallback policy")
                return result
            eligible = eligible[:1]
        used.add(portal.id)
        if not portal.resolved_url:
            reasons.append("Portal not resolved yet; playback may resolve it")
        if getattr(src, "enabled", True) is False:
            reasons.append(
                "Input source is deselected; explicit playlist links can still play"
            )
        if all(MANAGER.is_mac_busy(m.id) for m in eligible):
            reasons.append("All eligible MACs are busy (temporary)")
        if all(
            (m.status or "unknown").lower() in ("offline", "error", "unauthorized")
            for m in eligible
        ):
            reasons.append(
                "All eligible MACs have recorded connection/authentication errors; retryable"
            )
        if all(
            (expiry := parse_expiry(m.expire_date)) is not None and expiry <= now
            for m in eligible
        ):
            reasons.append(
                "All eligible accounts have a reported expiry in the past; recheck portal"
            )
        key = signature(src)
        observation = _OBSERVATIONS.get(key)
        evidence = (
            observation
            if observation and mono - observation[0] < observation[2]
            else None
        )
        if evidence:
            result["status"] = evidence[1]
            result["checked_at"] = now - timedelta(seconds=max(0, mono - evidence[0]))
            reasons.append(
                f"Recent {evidence[3]} succeeded"
                if evidence[1] == "healthy"
                else (
                    "Recent playback failed (source or output settings); probe input to isolate the cause"
                    if evidence[1] == "warning"
                    else "Recent media probe failed; retry before declaring it permanently down"
                )
            )
        else:
            reasons.append("Stream has not been verified recently")
        if reasons[:-1] and result["status"] != "failed":
            result["status"] = "warning"
        return result

    items = []
    specs = [
        (
            "live",
            LivePlaylist,
            LivePlaylistSource,
            LiveSource,
            "live_playlist_id",
            "live_source_id",
        ),
        (
            "vod",
            VodPlaylist,
            VodPlaylistSource,
            VodSource,
            "vod_playlist_id",
            "vod_source_id",
        ),
        (
            "series",
            SeriePlaylist,
            SeriePlaylistSource,
            SerieSource,
            "serie_playlist_id",
            "serie_source_id",
        ),
    ]
    for kind, model, link_model, source_model, parent_key, source_key in specs:
        playlists = (
            await db.scalars(
                select(model)
                .where(model.enabled.is_(True))
                .order_by(model.order, model.id)
            )
        ).all()
        links = await rows_for(
            db, link_model, getattr(link_model, parent_key), [p.id for p in playlists]
        )
        by_playlist = defaultdict(list)
        for link in sorted(links, key=lambda l: (l.priority, l.id)):
            by_playlist[getattr(link, parent_key)].append(getattr(link, source_key))
        sources = {
            s.id: s
            for s in await rows_for(
                db,
                source_model,
                source_model.id,
                [getattr(l, source_key) for l in links],
            )
        }
        if kind == "series":
            season_links = await rows_for(
                db,
                SeriePlaylistSeason,
                SeriePlaylistSeason.serie_playlist_id,
                [p.id for p in playlists],
            )
            selected = defaultdict(list)
            for link in season_links:
                if link.enabled:
                    selected[link.serie_playlist_id].append(link.serie_season_id)
            seasons = await rows_for(
                db,
                SerieSeason,
                SerieSeason.serie_source_id,
                set(sources) | {p.serie_source_id for p in playlists},
            )
            seasons_by_id = {s.id: s for s in seasons}
            episodes = await rows_for(
                db, SerieEpisode, SerieEpisode.serie_season_id, seasons_by_id
            )
            by_season = defaultdict(list)
            coordinates = {}
            for ep in sorted(episodes, key=lambda e: (e.episode_number, e.id)):
                by_season[ep.serie_season_id].append(ep)
                season = seasons_by_id[ep.serie_season_id]
                coordinates[
                    (season.serie_source_id, season.season_number, ep.episode_number)
                ] = ep
            owners = Counter(p.serie_source_id for p in playlists)
        for pl in playlists:
            out = {
                "id": pl.id,
                "kind": kind,
                "name": pl.custom_name,
                "group": pl.group_name or "",
                "status": "unavailable",
                "reasons": [],
                "sources": [],
                "source_count": 0,
            }
            source_ids = by_playlist[pl.id]
            if kind != "series":
                used = set()
                out["sources"] = [
                    candidate(
                        sources.get(sid),
                        getattr(sources.get(sid), "portal_id", None),
                        kind,
                        used=used,
                    )
                    for sid in source_ids
                ]
                out["status"] = aggregate(out["sources"])
                if not source_ids:
                    out["reasons"].append("No playback source links configured")
            else:
                states = []
                problem_episodes = []
                total = 0
                unavailable_episodes = 0
                if not selected[pl.id]:
                    out["reasons"].append("No enabled playlist seasons")
                if owners[pl.serie_source_id] > 1:
                    out["reasons"].append(
                        "Multiple enabled playlist series share this primary source; episode ownership is ambiguous"
                    )
                for season_id in selected[pl.id]:
                    season = seasons_by_id.get(season_id)
                    if not season or season.serie_source_id != pl.serie_source_id:
                        out["reasons"].append(
                            "An enabled season is missing or belongs to a different primary series"
                        )
                        states.append("unavailable")
                        continue
                    if not by_season[season_id]:
                        out["reasons"].append(
                            f"Season {season.season_number}: no episodes fetched"
                        )
                        states.append("unavailable")
                    for ep in by_season[season_id]:
                        total += 1
                        used = set()
                        candidates = []
                        for sid in source_ids:
                            src = sources.get(sid)
                            alt = coordinates.get(
                                (sid, season.season_number, ep.episode_number)
                            )
                            c = candidate(
                                alt,
                                getattr(src, "portal_id", None),
                                "episode",
                                used=used,
                                name=f'S{season.season_number:02d}E{ep.episode_number:02d} · {src.original_name if src else "Missing source"}',
                            )
                            if not alt:
                                c["reasons"] = [
                                    "Matching season/episode is missing from this fallback source"
                                ]
                            candidates.append(c)
                        status = aggregate(candidates)
                        if owners[pl.serie_source_id] > 1:
                            status = "unavailable"
                        states.append(status)
                        if status == "unavailable":
                            unavailable_episodes += 1
                        if status == "unavailable" and len(problem_episodes) < 20:
                            problem_episodes.append(
                                f"S{season.season_number:02d}E{ep.episode_number:02d}"
                            )
                        out["source_count"] += len(candidates)
                        out["sources"] = sorted(
                            out["sources"] + candidates,
                            key=lambda c: {
                                "unavailable": 0,
                                "failed": 0,
                                "warning": 1,
                                "unverified": 2,
                                "healthy": 3,
                                "skipped": 4,
                            }[c["status"]],
                        )[:40]
                out["episodes_checked"] = total
                out["episodes_unavailable"] = unavailable_episodes
                out["problem_episodes"] = problem_episodes
                if not source_ids:
                    out["reasons"].append("No playback source links configured")
                if states:
                    out["status"] = (
                        "unavailable"
                        if all(x == "unavailable" for x in states)
                        else (
                            "warning"
                            if any(x in ("unavailable", "warning") for x in states)
                            else "unverified" if "unverified" in states else "healthy"
                        )
                    )
                if problem_episodes:
                    out["reasons"].append(
                        "Episodes without a usable route: "
                        + ", ".join(problem_episodes)
                    )
            if kind != "series":
                out["source_count"] = len(out["sources"])
                out["sources"] = out["sources"][:40]
            out["reasons"] = list(dict.fromkeys(out["reasons"]))
            items.append(out)

    locals_ = (
        await db.scalars(
            select(LocalPlaylist)
            .where(LocalPlaylist.enabled.is_(True))
            .order_by(LocalPlaylist.order, LocalPlaylist.id)
        )
    ).all()
    files = {
        f.id: f
        for f in await rows_for(
            db, LocalFile, LocalFile.id, [p.local_file_id for p in locals_]
        )
    }
    directories = {
        d.id: d
        for d in await rows_for(
            db, LocalSource, LocalSource.id, [f.local_source_id for f in files.values()]
        )
    }
    paths = {
        fid: local_file_path(directories[f.local_source_id].directory, f.relative_path)
        for fid, f in files.items()
        if f.local_source_id in directories
    }
    disk = await inspect_files(paths.values())
    for pl in locals_:
        f = files.get(pl.local_file_id)
        path = paths.get(pl.local_file_id)
        reasons = []
        state = "healthy"
        source_state = None
        checked_at = None
        if not path:
            state = "unavailable"
            reasons.append("Local file or directory record is missing")
        elif path not in disk:
            state = "unverified"
            reasons.append("Filesystem check still pending or timed out")
        elif disk[path]:
            state = "unavailable"
            reasons.append(disk[path])
        else:
            reasons.append(
                "File exists, is readable and is not empty; media decoding is not verified"
            )
            observation = _OBSERVATIONS.get(signature(f, path))
            if (
                observation
                and mono - observation[0] < observation[2]
                and observation[1] == "failed"
            ):
                state = "unavailable"
                source_state = "failed"  # keep the explicit retry action available
                checked_at = now - timedelta(seconds=max(0, mono - observation[0]))
                reasons.append("Recent media probe failed")
            elif not f.enabled or not directories[f.local_source_id].enabled:
                state = "warning"
                reasons.append(
                    "Local input is deselected; explicit playlist files can still play"
                )
        items.append(
            {
                "id": pl.id,
                "kind": "local",
                "name": pl.custom_name or (f.filename if f else "Missing local file"),
                "group": pl.group_name or "",
                "status": state,
                "reasons": reasons,
                "source_count": int(bool(f)),
                "sources": (
                    [
                        {
                            "kind": "local",
                            "id": f.id,
                            "name": f.filename,
                            "portal": None,
                            "status": source_state or state,
                            "reasons": reasons,
                            "checked_at": checked_at,
                        }
                    ]
                    if f
                    else []
                ),
            }
        )
    counts = {
        kind: {
            "checked": 0,
            "unavailable": 0,
            "warning": 0,
            "unverified": 0,
            "healthy": 0,
        }
        for kind in KINDS
    }
    for item in items:
        counts[item["kind"]]["checked"] += 1
        counts[item["kind"]][item["status"]] += 1
    return {
        "checked_at": now,
        "counts": counts,
        "items": items,
        "evidence_ttl_seconds": EVIDENCE_TTL,
        "mode": "passive",
        "fallback_strategy": strategy,
    }
